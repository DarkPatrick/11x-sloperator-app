"""Daily autonomous preparation and review of one experiment design."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from typing import Protocol
from zoneinfo import ZoneInfo

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import HeadlessAgentRun
from sloperator.automated_session_policy import (
    AUTOMATED_ATLASSIAN_IDENTITY,
    AUTOMATED_RESPONSE_STYLE,
    AUTOMATED_SESSION_REPOSITORY_POLICY,
)
from sloperator.config import Settings
from sloperator.experiment_design_selector import (
    DesignCandidate,
    JiraRestReader,
    SelectionError,
    select_candidate,
)
from sloperator.jira_agent_policy import (
    REVIEWER_OWNERSHIP_POLICY,
    REVIEWER_START_POLICY,
    WORKER_JIRA_POLICY,
)

LOGGER = logging.getLogger(__name__)
NO_OP_RESULT = "No eligible experiment-design task was found."
PREPARED_RE = re.compile(r"DESIGN_PREPARED: (?P<task>UMN-\d+) \| (?P<epic>UMN-\d+)")
FAILURE_PREFIX = "Experiment design automation failed:"
TASK_LINK_RE = re.compile(r"mu--se\.atlassian\.net/browse/(?P<task>UMN-\d+)")

SELECTION_RULES = """\
Selection and pairing rules (strict and fail-closed):
1. Work only on Jira board 175 (`UMN Week Plan`). Read its current board configuration and filter;
   do not assume that status names or ids are permanent.
2. Candidate parents are issues visible on that board whose issue type is Epic, current board column
   is In Progress, and current component is `Project - Hypothesis`. This component is the marker
   added by the Hypothesis automation.
3. Under every candidate epic, find ordinary tasks whose normalized summary contains either
   `Проектирование и Питч` or `Расчет сверху и план тестирования`. Matching is case-insensitive,
   normalizes the Russian yo/ye spelling, collapses whitespace, and is a substring match rather
   than an exact-title match. There may be several iterations and several tasks of each kind.
4. Pair iterations inside the same epic by the automation creation batch, not by the current title
   suffix: the Pitch task is created immediately before its calculation task. Pair a calculation
   task with the closest earlier Pitch task created in the same epic within 60 seconds. Prefer the
   immediately preceding Jira key as corroboration, but do not require adjacent keys because the
   template may create another issue between them. Enforce one-to-one pairing. If timestamps make a
   pair ambiguous, exclude it rather than guessing. This survives later independent renames; for
   example UMN-12843 and UMN-12844 are one pair despite different iteration suffixes.
5. Derive status eligibility from the board columns, not status categories. The calculation task
   must currently be in the Backlog or To Do column. Its paired Pitch task must currently be in the
   In Review or Done column. The Done column includes every status currently mapped to it, including
   `No need`, as well as statuses such as Done/Готово, Success, or Fail.
6. Read the paired Pitch task changelog. Find its most recent transition into an In Review/Done
   status while accounting for the current board-column mapping. It is eligible only when that
   transition happened in the closed interval [now minus one calendar month, now] in Asia/Nicosia.
   Missing, future, ambiguous, or older transitions are ineligible. Do not use issue `updated` as a
   substitute for the status-transition timestamp.
7. Order eligible calculation tasks by their Jira `created` timestamp, then numeric issue key as a
   deterministic tie-breaker, and select exactly the oldest one. Re-fetch the epic, both paired
   tasks, board configuration, and Pitch changelog immediately before the first outward write. If
   eligibility changed, re-run selection; never work on a stale or guessed candidate.
"""

PREPARATION_PROMPT = f"""\
[claude]
This is the authorised daily autonomous UG experiment-design preparation job. You cannot
communicate with a human in this session: do not ask questions, wait for approval, or offer choices.
Complete the work autonomously. When evidence is uncertain, use explicit, internally consistent
assumptions and, where useful, publish a small set of carefully labelled scenarios such as
Realistic and Pessimistic. Never invent measured data.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_ATLASSIAN_IDENTITY}

{AUTOMATED_RESPONSE_STYLE}

Use the `ug-experiment-design-power` skill for the entire design workflow, including every required
neighboring skill, reference, SQL gate, freshness check, ClickHouse query, Redash publication,
calculation helper, Confluence builder, and post-write verification. The scheduled job explicitly
authorises the Redash and Confluence writes required by that skill for the selected task. Preserve
unrelated page content and other iterations.

Sloperator selects the Jira candidate deterministically before launching you. Do not search for or
substitute another candidate. The exact selected keys will be appended to this prompt at runtime.
When no candidate is eligible, Sloperator stops before launching an agent, so this prompt is never
used for an empty selection.

{SELECTION_RULES}
These are pre-start selection rules. The reviewer has claimed the selected task: its expected
current status is now In Progress. Do not reselect from the remaining queue or reject this expected
status change. Re-check the exact epic, pair, and iteration; all other eligibility gates still apply.

Execution:
1. The reviewer has already started this exact task. Read and verify its In Progress status,
   bot assignee, and Start date; do not change Jira. If the verified start is missing, stop with
   `{FAILURE_PREFIX} Jira start verification failed`.
{WORKER_JIRA_POLICY}
2. For the selected calculation task, resolve the correct project page and the matching iteration.
   Use the skill's strict formats to fully calculate, build, and populate both `Reach & Impact` and
   `Experiment design`. Follow the skill's monetisation-first defaults, mature-cohort rules, exact
   metric naming, saved-Redash-query requirements, table builders, and rendered-structure checks.
3. You are the preparation pass only. Do not comment on Jira or send a
   Slack message. Leave those actions to an independent reviewer.
4. Re-fetch the page and verify both blocks are non-empty, belong to the selected iteration, contain
   the required links and strict table structure, and did not remove unrelated content.
5. On success return exactly `DESIGN_PREPARED: <calculation task key> | <epic key>` on one line.
   On failure return one concise line beginning exactly `{FAILURE_PREFIX}`. Do not return progress,
   an audit, calculations, or any other text.
"""


def preparation_prompt(candidate: DesignCandidate) -> str:
    """Bind the deterministic Jira selection to the preparation agent."""
    return f"""{PREPARATION_PROMPT}

Authoritative deterministic selection:
- calculation task: `{candidate.task_key}`
- paired Pitch task: `{candidate.pitch_key}`
- epic: `{candidate.epic_key}`

Work only on this exact task, pair, epic, and matching project-page iteration. If any current Jira
fact contradicts this selection, make no writes and return `{FAILURE_PREFIX} selection changed`.
Your success marker must contain `{candidate.task_key} | {candidate.epic_key}`.
"""


def start_prompt(candidate: DesignCandidate) -> str:
    return f"""[claude]
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{AUTOMATED_RESPONSE_STYLE}
{REVIEWER_OWNERSHIP_POLICY}

Start only task {candidate.task_key}, paired Pitch {candidate.pitch_key}, epic {candidate.epic_key}.
{SELECTION_RULES}
{REVIEWER_START_POLICY}
Do not prepare the deliverable in this pass. After verifying the start, return exactly:
EXPERIMENT_TASK_STARTED: {candidate.task_key} | {candidate.epic_key} | {candidate.pitch_key}
On failure return one line starting with {FAILURE_PREFIX}.
"""


def started_candidate(text: str) -> DesignCandidate | None:
    match = re.fullmatch(
        r"EXPERIMENT_TASK_STARTED: (UMN-\d+) \| (UMN-\d+) \| (UMN-\d+)", text.strip()
    )
    if match is None:
        return None
    return DesignCandidate(match[1], match[2], match[3], "", "")


def review_prompt(task_key: str, epic_key: str) -> str:
    """Build the independent second-pass prompt for one prepared task."""
    return f"""\
[claude]
This is the authorised independent review pass for an autonomously prepared UG experiment design.
During this autonomous pass you cannot communicate with a human: do not ask questions, wait for
approval, or offer choices.
Another agent has already populated the project page for calculation task `{task_key}` in epic
`{epic_key}`. Review that exact task and iteration, correct every issue you find, and complete the
whole workflow autonomously.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_ATLASSIAN_IDENTITY}

{AUTOMATED_RESPONSE_STYLE}

Use the `ug-experiment-design-power` skill in its review/validation mode, including every required
neighboring skill, reference, SQL gate, source-query execution, baseline reconciliation, table
builder, and rendered-page verification. The scheduled job explicitly authorises necessary Redash,
Confluence, Jira comment, and Jira transition writes for this exact task. Do not trust the first
agent's calculations merely because they are already on the page: open and run the linked sources,
reconcile every cell, verify cohort denominators and maturity, and correct the page when needed.
When uncertainty cannot be eliminated, preserve clearly labelled, internally consistent scenarios
such as Realistic and Pessimistic rather than inventing certainty.

Session ownership after publication:
- On success Sloperator attaches this same session to the Slack thread containing your notification.
  The no-human constraint above ends when the autonomous pass is published; respond normally to
  subsequent user messages in that thread.
- In that conversation, own the final corrected solution on the project page as the responsible
  author. You are accountable for its calculations, evidence, assumptions, structure, and
  recommendations even though another agent prepared the first draft.
- Never describe yourself as merely a reviewer, distance yourself from the implementation, or hand
  responsibility back to the preparation agent. Answer questions about why the solution was built
  this way, re-check its sources when challenged, and make requested in-scope corrections to the
  project-page design under the same data-quality and publication safeguards.

Before any write, re-fetch the exact task, epic, pair, and iteration. The pre-start selection
rules below remain applicable except that the claimed task is now In Progress, so it must not be
reselected from the remaining queue or compared to a new oldest queued task. On recovery, if it
is already In Review or Done, verify the existing result and avoid duplicate comments or backward
transitions; preserve completed fields. Fail if the task, pair, or scope changed unexpectedly.
{SELECTION_RULES}

After the page is correct and verified:
1. Use the repository Jira helper to add one short English comment to `{task_key}` saying that
   I calculated and published Reach & Impact and Experiment design,
   with the project-page link. Re-fetch the issue and verify the comment.
2. Transition `{task_key}` using transition ID `181` (target status `In Review`) and verify the resulting
   status. The verified Jira field ID for `Due date` is `duedate`; set it to today's date in `YYYY-MM-DD`
   format. Re-fetch the issue and verify the final status and due date. If any write or verification fails,
   return `{FAILURE_PREFIX} Jira review update failed`.
3. Resolve Slack user ids from authoritative Slack profiles for the epic assignee and, when set,
   the calculation-task assignee. Deduplicate the mentions. Never guess Slack ids. If a Jira
   assignee cannot be resolved, use their plain display name and keep the notification concise.
4. Do not send Slack messages yourself. Return exactly one Slack-ready notification for Sloperator
   to publish in `ug-monetization-pvt`: one compact line containing the resolved mentions/names, a
   link to `{task_key}`, and a short statement that both design blocks are ready and asking the
   assignees to check them. Do not include calculations, evidence, Jira-transition details, raw
   URLs, headings, bullets, or an operational appendix.

On failure return one concise line beginning exactly `{FAILURE_PREFIX}`.
"""


class AgentSubmitter(Protocol):
    async def execute_once(
        self,
        text: str,
        timeout_seconds: int,
        *,
        job_name: str = "scheduled-agent",
        accept_result: Callable[[str], bool] = lambda _: True,
        max_interim_results: int = 2,
        existing_session_id: str | None = None,
    ) -> HeadlessAgentRun: ...

    async def attach_session(
        self,
        channel_id: str,
        thread_ts: str,
        run: HeadlessAgentRun,
    ) -> None: ...


class InvalidDesignResult(ValueError):
    """The agent returned an unexpected preparation or review result."""


def parse_preparation_result(text: str) -> tuple[str, str] | None:
    """Extract one unambiguous terminal result despite surrounding agent prose."""
    stripped = text.strip()
    prepared = {
        (match.group("task"), match.group("epic")) for match in PREPARED_RE.finditer(stripped)
    }
    no_op = any(line.strip() == NO_OP_RESULT for line in stripped.splitlines())
    failures = [
        line.strip() for line in stripped.splitlines() if line.strip().startswith(FAILURE_PREFIX)
    ]
    terminal_kinds = bool(prepared) + no_op + bool(failures)
    if terminal_kinds > 1 or len(prepared) > 1:
        raise InvalidDesignResult("Preparation agent returned ambiguous terminal markers")
    if no_op:
        return None
    if prepared:
        return next(iter(prepared))
    if failures:
        raise InvalidDesignResult(failures[0])
    raise InvalidDesignResult("Preparation agent returned no valid terminal marker")


def is_preparation_result(text: str) -> bool:
    """Return whether a preparation response is terminal rather than interim."""
    try:
        parse_preparation_result(text)
    except InvalidDesignResult as error:
        return str(error).startswith(FAILURE_PREFIX)
    return True


def normalize_review_notification(text: str, task_key: str) -> str:
    """Extract and validate the single Slack-ready line produced by the reviewer."""
    stripped = text.strip()
    failure_lines = [
        line.strip() for line in stripped.splitlines() if line.strip().startswith(FAILURE_PREFIX)
    ]
    candidates = {
        line.strip()
        for line in stripped.splitlines()
        if f"browse/{task_key}" in line and ("check" in line.lower() or "провер" in line.lower())
    }
    if failure_lines and candidates:
        raise InvalidDesignResult("Review agent returned ambiguous terminal results")
    if failure_lines:
        return failure_lines[0]
    if len(candidates) != 1:
        raise InvalidDesignResult("Review agent returned no valid compact task notification")
    return next(iter(candidates))


def review_result_validator(task_key: str) -> Callable[[str], bool]:
    """Build an interim-result predicate bound to the selected task."""

    def validate(text: str) -> bool:
        try:
            normalize_review_notification(text, task_key)
        except InvalidDesignResult:
            return False
        return True

    return validate


def task_key_from_review_result(text: str) -> str:
    """Extract the calculation task from a recovered reviewer result."""
    stripped = text.strip()
    if stripped.startswith(FAILURE_PREFIX):
        raise InvalidDesignResult(stripped)
    match = TASK_LINK_RE.search(stripped)
    if match is None:
        raise InvalidDesignResult("Recovered reviewer result has no Jira task link")
    task_key = match.group("task")
    normalize_review_notification(stripped, task_key)
    return task_key


def is_review_result(text: str) -> bool:
    if started_candidate(text) is not None:
        return True
    """Return whether a reviewer response can be recovered without prior task state."""
    try:
        task_key_from_review_result(text)
    except InvalidDesignResult as error:
        return str(error).startswith(FAILURE_PREFIX)
    return True


def next_run_at(
    now: dt.datetime,
    timezone_name: str = "Asia/Nicosia",
    hour: int = 15,
) -> dt.datetime:
    """Return the next weekday wall-clock run time, preserving Cyprus DST."""
    timezone = ZoneInfo(timezone_name)
    local_now = now.astimezone(timezone)
    candidate = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += dt.timedelta(days=1)
    return candidate


async def publish_notification(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    run: HeadlessAgentRun,
    task_key: str,
) -> str:
    """Publish one verified reviewer notification and attach its session."""
    notification = normalize_review_notification(run.text, task_key)
    published_run = replace(run, text=notification)
    response = await client.chat_postMessage(
        channel=settings.experiment_design_channel,
        markdown_text=notification,
        unfurl_links=False,
        unfurl_media=False,
    )
    channel_id = response.get("channel", settings.experiment_design_channel)
    await agent.attach_session(channel_id, response["ts"], published_run)
    return notification


async def publish_failure(
    client: AsyncWebClient,
    settings: Settings,
    message: str,
) -> None:
    """Make an actual automation failure visible without treating a no-op as failure."""
    await client.chat_postMessage(
        channel=settings.experiment_design_channel,
        markdown_text=message,
        unfurl_links=False,
        unfurl_media=False,
    )


async def run_review(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    task_key: str,
    epic_key: str,
    reviewer_session_id: str | None = None,
) -> str:
    """Run and publish the independent review pass."""
    run = await agent.execute_once(
        review_prompt(task_key, epic_key),
        settings.experiment_design_timeout_seconds,
        job_name="experiment-design-reviewer",
        accept_result=review_result_validator(task_key),
        existing_session_id=reviewer_session_id,
    )
    return await publish_notification(client, agent, settings, run, task_key)


async def run_once(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    selector: Callable[[Settings], Awaitable[DesignCandidate | None]] | None = None,
) -> str | None:
    """Run preparation, stay silent on no-op, then run independent review."""
    choose = selector or select_from_jira
    selected = await choose(settings)
    if selected is None:
        LOGGER.info("No eligible experiment-design task; finishing silently")
        return None
    start_run = await agent.execute_once(
        start_prompt(selected), settings.experiment_design_timeout_seconds,
        job_name="experiment-design-reviewer",
        accept_result=lambda text: started_candidate(text) is not None or text.strip().startswith(FAILURE_PREFIX),
    )
    started = started_candidate(start_run.text)
    if started is None:
        raise InvalidDesignResult(start_run.text)
    if (started.task_key, started.epic_key, started.pitch_key) != (selected.task_key, selected.epic_key, selected.pitch_key):
        raise InvalidDesignResult("Reviewer start does not match deterministic selection")
    return await run_preparation(client, agent, settings, selected, start_run.session_id)


async def run_preparation(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    selected: DesignCandidate,
    reviewer_session_id: str,
) -> str:
    prepared_run = await agent.execute_once(
        preparation_prompt(selected),
        settings.experiment_design_timeout_seconds,
        job_name="experiment-design-preparer",
        accept_result=is_preparation_result,
    )
    try:
        prepared = parse_preparation_result(prepared_run.text)
    except InvalidDesignResult as error:
        if str(error).startswith(FAILURE_PREFIX):
            await publish_failure(client, settings, str(error))
        raise
    if prepared is None:
        raise InvalidDesignResult("Preparation agent contradicted deterministic selection")
    if prepared != (selected.task_key, selected.epic_key):
        raise InvalidDesignResult("Preparation agent returned keys outside deterministic selection")
    confirmed = await select_from_jira(settings, claimed_task_key=selected.task_key)
    if confirmed is None or (confirmed.task_key, confirmed.epic_key, confirmed.pitch_key) != (
        selected.task_key, selected.epic_key, selected.pitch_key
    ):
        raise InvalidDesignResult("Claimed Jira task or pairing changed before review")
    return await run_review(client, agent, settings, *prepared, reviewer_session_id=reviewer_session_id)


async def select_from_jira(
    settings: Settings, *, claimed_task_key: str | None = None,
) -> DesignCandidate | None:
    """Build the read-only Jira client and select one candidate."""
    if not settings.jira_username or not settings.jira_api_token:
        raise SelectionError("Jira credentials are unavailable to experiment-design selector")
    return await select_candidate(
        JiraRestReader(settings.jira_url, settings.jira_username, settings.jira_api_token),
        claimed_task_key=claimed_task_key,
    )


async def run_daily(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    enabled: Callable[[], bool] = lambda: True,
) -> None:
    """Run forever at the configured local wall-clock hour on weekdays."""
    while True:
        now = dt.datetime.now(dt.UTC)
        target = next_run_at(
            now,
            settings.experiment_design_timezone,
            settings.experiment_design_hour,
        )
        delay = (target.astimezone(dt.UTC) - now).total_seconds()
        LOGGER.info("Next experiment design run scheduled for %s", target.isoformat())
        await asyncio.sleep(delay)
        if not enabled():
            LOGGER.info("Scheduled experiment design run disabled from admin")
            continue
        run_task: asyncio.Task[str | None] | None = None
        try:
            LOGGER.info("Starting scheduled experiment design run")
            run_task = asyncio.create_task(
                run_once(client, agent, settings),
                name="scheduled-experiment-design-run",
            )
            await run_task
            LOGGER.info("Experiment design run completed")
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                if run_task is not None:
                    run_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await run_task
                raise
            LOGGER.info("Scheduled experiment design run cancelled from admin")
        except Exception:
            LOGGER.exception("Could not complete the daily experiment design run")


async def cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel the scheduler task during application shutdown."""
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
