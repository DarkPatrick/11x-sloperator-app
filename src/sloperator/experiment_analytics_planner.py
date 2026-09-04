"""Daily autonomous preparation and review of one experiment analytics specification."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import HeadlessAgentRun
from sloperator.automated_session_policy import (
    AUTOMATED_ATLASSIAN_IDENTITY,
    AUTOMATED_RESPONSE_STYLE,
    AUTOMATED_SESSION_REPOSITORY_POLICY,
)
from sloperator.config import Settings
from sloperator.experiment_design_planner import AgentSubmitter, next_run_at
from sloperator.experiment_design_selector import (
    ANALYTICS_TITLE,
    DesignCandidate,
    JiraRestReader,
    SelectionError,
    select_candidate,
)

LOGGER = logging.getLogger(__name__)
NO_OP_RESULT = "No eligible experiment-analytics task was found."
PREPARED_RE = re.compile(r"ANALYTICS_PREPARED: (?P<task>UMN-\d+) \| (?P<epic>UMN-\d+)")
FAILURE_PREFIX = "Experiment analytics automation failed:"
TASK_LINK_RE = re.compile(r"mu--se\.atlassian\.net/browse/(?P<task>UMN-\d+)")

SELECTION_RULES = """\
Selection and pairing rules (strict and fail-closed):
1. Work only on Jira board 175 (`UMN Week Plan`). Read its current board configuration and filter;
   do not assume that status names or ids are permanent.
2. Candidate parents are issues visible on that board whose issue type is Epic, current board column
   is In Progress, and current component is `Project - Hypothesis`.
3. Under every candidate epic, find ordinary tasks whose normalized summary contains either
   `Проектирование и Питч` or `Аналитика`. Matching is case-insensitive, normalizes Russian
   yo/ye spelling, collapses whitespace, and is a substring match. There may be several iterations.
4. Pair iterations inside the same epic by the automation creation batch: pair each Analytics task
   with the closest earlier Pitch task created in the same epic within 60 seconds. Enforce
   one-to-one pairing and exclude ambiguous pairs instead of guessing.
5. Derive eligibility from board columns. The Analytics task must be in Backlog or To Do; its paired
   Pitch task must be in In Review or Done, including every status currently mapped to Done.
6. The Pitch task's most recent transition into In Review/Done must be in the closed interval
   [now minus one calendar month, now] in Asia/Nicosia. Do not substitute issue `updated`.
7. Order eligible Analytics tasks by Jira `created`, then numeric issue key, and select the oldest.
   Re-fetch the epic, pair, board configuration, and Pitch changelog before the first outward write.
"""

PREPARATION_PROMPT = f"""\
[claude]
This is the authorised daily autonomous UG experiment-analytics preparation job. You cannot
communicate with a human during this pass: do not ask questions, wait for approval, or offer
choices. Complete the work autonomously and never invent product behaviour or analytics events.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_ATLASSIAN_IDENTITY}

{AUTOMATED_RESPONSE_STYLE}

Use the `ug-analytics-spec-writer` skill for the entire workflow, including every required
neighboring skill, reference, source-of-truth check, project-page builder, and post-write
verification. The scheduled job explicitly authorises the Confluence writes required by that skill
for the selected task. Preserve unrelated page content and other iterations.

Sloperator selects the Jira candidate deterministically before launching you. Do not search for or
substitute another candidate. The exact selected keys are appended at runtime.

{SELECTION_RULES}

Execution:
1. For the selected Analytics task, resolve the correct project page and matching iteration. Build
   and populate the complete analytics specification through `ug-analytics-spec-writer`, using its
   required structure, naming, source validation, implementation details, and verification gates.
2. You are the preparation pass only. Do not comment on Jira, transition an issue, or send Slack.
3. Re-fetch the page and verify the analytics specification is complete, belongs to the selected
   iteration, and did not remove unrelated content.
4. On success return exactly `ANALYTICS_PREPARED: <Analytics task key> | <epic key>` on one line.
   On failure return one concise line beginning exactly `{FAILURE_PREFIX}`.
"""


def preparation_prompt(candidate: DesignCandidate) -> str:
    return f"""{PREPARATION_PROMPT}

Authoritative deterministic selection:
- Analytics task: `{candidate.task_key}`
- paired Pitch task: `{candidate.pitch_key}`
- epic: `{candidate.epic_key}`

Work only on this exact task, pair, epic, and matching project-page iteration. If any current Jira
fact contradicts the selection, make no writes and return `{FAILURE_PREFIX} selection changed`.
Your success marker must contain `{candidate.task_key} | {candidate.epic_key}`.
"""


def review_prompt(task_key: str, epic_key: str) -> str:
    return f"""\
[claude]
This is the authorised independent review pass for an autonomously prepared UG analytics
specification. During this autonomous pass you cannot communicate with a human. Another agent has
populated the project page for Analytics task `{task_key}` in epic `{epic_key}`. Independently
review that exact task and iteration, correct every issue, and complete the workflow autonomously.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_ATLASSIAN_IDENTITY}

{AUTOMATED_RESPONSE_STYLE}

Use `ug-analytics-spec-writer` in review/validation mode, including all required sources, naming and
coverage checks, page-builder rules, and rendered-page verification. The job authorises necessary
Confluence, Jira comment, and Jira transition writes for this exact task. Verify the specification
from authoritative product and analytics sources; do not trust the first draft merely because it
is already published.

Session ownership after publication:
- On success Sloperator attaches this same session to the Slack notification thread. The no-human
  constraint ends after publication; respond normally to subsequent user messages.
- Own the final corrected analytics specification as the responsible author. Be accountable for
  its event model, properties, triggers, naming, evidence, assumptions, and recommendations.
- Never describe yourself as merely a reviewer or hand responsibility to the preparation agent.
  Re-check sources when challenged and make requested in-scope corrections under the same gates.

Before any write, independently verify that `{task_key}` is still the oldest eligible task under:
{SELECTION_RULES}
If eligibility changed, make no further writes and fail; never substitute another task.

After the page is correct and verified:
1. Add one short English Jira comment to `{task_key}` through the repository helper saying the
   analytics specification was prepared, published, and independently reviewed, with the page link.
   Re-fetch and verify the comment.
2. Transition `{task_key}` to the status dynamically mapped to the board's In Review column and
   verify the result; do not hardcode a transition id.
3. Resolve Slack identities for the epic assignee and Analytics-task assignee. Deduplicate mentions,
   never guess ids, and fall back to plain display names when necessary.
4. Do not send Slack yourself. Return exactly one compact Slack-ready line with the mentions/names,
   a link to `{task_key}`, and a statement that the analytics specification is ready asking them to
   check it. No calculations, evidence, transition details, raw URLs, headings, or bullets.

On failure return one concise line beginning exactly `{FAILURE_PREFIX}`.
"""


class InvalidAnalyticsResult(ValueError):
    """The analytics agent returned an unsafe or ambiguous terminal result."""


def parse_preparation_result(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    prepared = {(m.group("task"), m.group("epic")) for m in PREPARED_RE.finditer(stripped)}
    no_op = any(line.strip() == NO_OP_RESULT for line in stripped.splitlines())
    failures = [
        line.strip()
        for line in stripped.splitlines()
        if line.strip().startswith(FAILURE_PREFIX)
    ]
    if bool(prepared) + no_op + bool(failures) > 1 or len(prepared) > 1:
        raise InvalidAnalyticsResult("Preparation agent returned ambiguous terminal markers")
    if no_op:
        return None
    if prepared:
        return next(iter(prepared))
    if failures:
        raise InvalidAnalyticsResult(failures[0])
    raise InvalidAnalyticsResult("Preparation agent returned no valid terminal marker")


def is_preparation_result(text: str) -> bool:
    try:
        parse_preparation_result(text)
    except InvalidAnalyticsResult as error:
        return str(error).startswith(FAILURE_PREFIX)
    return True


def normalize_review_notification(text: str, task_key: str) -> str:
    stripped = text.strip()
    failures = [
        line.strip()
        for line in stripped.splitlines()
        if line.strip().startswith(FAILURE_PREFIX)
    ]
    candidates = {
        line.strip()
        for line in stripped.splitlines()
        if f"browse/{task_key}" in line and ("check" in line.lower() or "провер" in line.lower())
    }
    if (failures and candidates) or len(failures) > 1 or len(candidates) > 1:
        raise InvalidAnalyticsResult("Review agent returned ambiguous terminal results")
    if failures:
        return failures[0]
    if not candidates:
        raise InvalidAnalyticsResult("Review agent returned no valid notification")
    result = next(iter(candidates))
    links = set(TASK_LINK_RE.findall(result))
    if links != {task_key} or "analytics" not in result.lower() or "\n" in result:
        raise InvalidAnalyticsResult("Review notification has an invalid task or format")
    return result


def review_result_validator(task_key: str) -> Callable[[str], bool]:
    def validate(text: str) -> bool:
        try:
            normalize_review_notification(text, task_key)
        except InvalidAnalyticsResult:
            return text.strip().startswith(FAILURE_PREFIX)
        return True

    return validate


def task_key_from_review_result(text: str) -> str:
    if text.strip().startswith(FAILURE_PREFIX):
        raise InvalidAnalyticsResult(text.strip())
    match = TASK_LINK_RE.search(text)
    if match is None:
        raise InvalidAnalyticsResult("Recovered reviewer result has no Jira task link")
    task_key = match.group("task")
    normalize_review_notification(text, task_key)
    return task_key


def is_review_result(text: str) -> bool:
    try:
        task_key_from_review_result(text)
    except InvalidAnalyticsResult as error:
        return str(error).startswith(FAILURE_PREFIX)
    return True


async def publish_notification(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    run: HeadlessAgentRun,
    task_key: str,
) -> str:
    notification = normalize_review_notification(run.text, task_key)
    response = await client.chat_postMessage(
        channel=settings.experiment_analytics_channel,
        markdown_text=notification,
        unfurl_links=False,
        unfurl_media=False,
    )
    await agent.attach_session(
        response.get("channel", settings.experiment_analytics_channel),
        response["ts"],
        replace(run, text=notification),
    )
    return notification


async def publish_failure(client: AsyncWebClient, settings: Settings, message: str) -> None:
    await client.chat_postMessage(
        channel=settings.experiment_analytics_channel,
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
) -> str:
    run = await agent.execute_once(
        review_prompt(task_key, epic_key),
        settings.experiment_analytics_timeout_seconds,
        job_name="experiment-analytics-reviewer",
        accept_result=review_result_validator(task_key),
    )
    return await publish_notification(client, agent, settings, run, task_key)


async def run_once(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    selector: Callable[[Settings], Awaitable[DesignCandidate | None]] | None = None,
) -> str | None:
    choose = selector or select_from_jira
    selected = await choose(settings)
    if selected is None:
        LOGGER.info("No eligible experiment-analytics task; finishing silently")
        return None
    prepared_run = await agent.execute_once(
        preparation_prompt(selected),
        settings.experiment_analytics_timeout_seconds,
        job_name="experiment-analytics-preparer",
        accept_result=is_preparation_result,
    )
    try:
        prepared = parse_preparation_result(prepared_run.text)
    except InvalidAnalyticsResult as error:
        if str(error).startswith(FAILURE_PREFIX):
            await publish_failure(client, settings, str(error))
        raise
    if prepared is None:
        raise InvalidAnalyticsResult("Preparation agent contradicted deterministic selection")
    if prepared != (selected.task_key, selected.epic_key):
        raise InvalidAnalyticsResult("Preparation result does not match deterministic selection")
    confirmed = await choose(settings)
    if confirmed != selected:
        raise InvalidAnalyticsResult("Deterministic Jira selection changed before review")
    return await run_review(client, agent, settings, *prepared)


async def select_from_jira(settings: Settings) -> DesignCandidate | None:
    if not settings.jira_username or not settings.jira_api_token:
        raise SelectionError("Jira credentials are unavailable to experiment-analytics selector")
    return await select_candidate(
        JiraRestReader(settings.jira_url, settings.jira_username, settings.jira_api_token),
        task_title=ANALYTICS_TITLE,
    )


async def run_daily(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    enabled: Callable[[], bool] = lambda: True,
) -> None:
    while True:
        now = dt.datetime.now(dt.UTC)
        target = next_run_at(
            now, settings.experiment_analytics_timezone, settings.experiment_analytics_hour
        )
        LOGGER.info("Next experiment analytics run scheduled for %s", target.isoformat())
        await asyncio.sleep((target.astimezone(dt.UTC) - now).total_seconds())
        if not enabled():
            LOGGER.info("Scheduled experiment analytics run disabled from admin")
            continue
        run_task: asyncio.Task[str | None] | None = None
        try:
            LOGGER.info("Starting scheduled experiment analytics run")
            run_task = asyncio.create_task(
                run_once(client, agent, settings), name="scheduled-experiment-analytics-run"
            )
            await run_task
            LOGGER.info("Experiment analytics run completed")
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                if run_task is not None:
                    run_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await run_task
                raise
            LOGGER.info("Scheduled experiment analytics run cancelled from admin")
        except Exception:
            LOGGER.exception("Could not complete the daily experiment analytics run")


async def cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
