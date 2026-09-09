"""Daily autonomous finalisation of one recent monetisation experiment."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Callable
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
from sloperator.jira_agent_policy import (
    REVIEWER_OWNERSHIP_POLICY,
    REVIEWER_START_POLICY,
    WORKER_JIRA_POLICY,
)

LOGGER = logging.getLogger(__name__)
NO_OP_PREFIX = "No eligible experiment"
NO_OP_NOTIFICATION = "No eligible experiment was found for calculation today."
FAILURE_PREFIXES = (
    "Experiment finalisation failed:",
    "Experiment finalization failed:",
)

SELECTION_RULES = f"""\
Selection rules:
1. Start exclusively from `ug_experiment_calculator.get_ugm_exps_list(config=cfg)`. Treat the
   returned ids as the authoritative allowlist: never inspect, calculate, select, or publish an
   experiment whose id is absent from that list. Do not use the general experiment list and then
   infer the domain from metrics, clients, segments, a project page, or other metadata. If the UGM
   allowlist lookup fails or cannot be verified, fail closed with no eligible experiment.
   As an independent sanity check, require the experiment title (case-insensitively) to contain
   `UG Monetization`, `UG Monetisation`, or the Russian stem `монетизац`. Absence of all
   three markers makes the experiment ineligible even when its id appears in the UGM allowlist.
   Apply both checks before inspecting project pages and before invoking the calculator.
2. Use the UG experiment admin/source of truth to find experiments whose actual experiment end
   timestamp is in the closed interval [now minus one calendar month, now]. Include only
   experiments that have already ended; exclude running, scheduled, paused without an actual
   end, or otherwise unfinished experiments.
3. Apply a strict, fail-closed post-stop age gate before invoking the calculator or reading any
   calculation rows. Compute elapsed age from the experiment's actual end timestamp to the current
   time: every app-only experiment must have been stopped for at least 8 complete days (8 * 24
   hours), while every experiment with a web client (`UG_WEB` or `UG_MOBWEB`) must have been stopped
   for at least 9 complete days (9 * 24 hours). For a mixed app+web experiment use the stricter
   9-day threshold. An exact threshold value passes. A younger experiment, or one with a missing,
   ambiguous, future, or unverifiable actual end timestamp/client classification, is ineligible and
   must not be calculated during this run.
4. Keep only experiments that have at least one configured segment in the admin.
5. Locate each experiment's project page and exclude it if the final Results/Итоги section for
   this experiment/iteration is already populated. Do not mistake a template, empty placeholder,
   design table, or results for another iteration for completed итогов.
6. Locate the exact Jira Results/Итоги task for the matching experiment and iteration before any
   calculator invocation or calculation-row inspection. Exclude the experiment when that task is
   already In Progress, In Review, Done, or any other non-queued status; only a task in Backlog or
   To Do may enter the candidate pool. If the exact task or its status cannot be established, exclude
   the candidate and continue to the next experiment. Re-check this status immediately before any
   write as well.
7. The experiments remaining after rules 1-6 form the preliminary candidate pool. Order the whole
   pool by actual end timestamp, then by experiment id as a deterministic tie-breaker. If the pool
   is empty, stop immediately: do not invoke the calculator, do not inspect fallback experiments
   outside these rules, make no Confluence or Jira writes, and return exactly
   `{NO_OP_NOTIFICATION}` and nothing else.
8. Walk the ordered preliminary candidate pool from oldest to newest. For each candidate, obtain
   freshly calculated maturity data. Use results produced by this run; if its calculator rows are
   not demonstrably fresh, recalculate it first using the direct-library procedure below and verify
   the fresh rows. Never determine eligibility from stale cached results. Apply a
   strict, fail-closed pending-trials gate to that candidate. An
   experiment is eligible only when `pending trials, %` is present and strictly below 5% in every
   applicable variation row for every configured client and segment. A value equal to or above 5%,
   or a missing, null, stale, failed, or unverifiable value, excludes only that candidate before any
   Results/Insights/Decision generation and before any Confluence or Jira write. When a candidate
   is excluded by this gate, continue to the next candidate in the ordered pool and calculate its
   fresh maturity data. Stop iterating at the first candidate that passes every eligibility rule;
   select and finalise exactly that experiment. Pending charges are explicitly not an eligibility
   condition: report incomplete charge maturity as a caveat in the published Results/Insights, but
   do not exclude an otherwise eligible experiment for it. Only if every candidate in the pool has
   been checked and excluded may you return exactly `{NO_OP_NOTIFICATION}` and nothing else. Do not
   treat this expected no-op as an error.
9. Immediately before any write, re-fetch the UGM allowlist and experiment title and re-check both
   monetisation gates from rule 1, the actual end timestamp/client classification and age gate, the
   admin/page conditions, and the strict pending-trials gate against the same fresh
   calculation. If the experiment is no longer eligible, make no Confluence or Jira writes and
   return exactly `{NO_OP_NOTIFICATION}` and nothing else. Keep filter/audit details internal.

"""

FINALIZATION_PROMPT = f"""\
[claude]
This is the authorised daily autonomous experiment-finalisation preparation pass. Complete the
calculation and Confluence publication in this turn. The user explicitly pre-approves progression through all three
publication stages (Results, then Insights, then Decision / Next steps), including the required
Confluence update. All Jira writes belong exclusively to the reviewer. Do not pause to request approval between stages. This
instruction intentionally overrides only the interactive approval pauses in the skills; keep
all their data-quality, maturity, verification, language, and publication safeguards.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_ATLASSIAN_IDENTITY}

{AUTOMATED_RESPONSE_STYLE}

Goal: finalise exactly one eligible UG monetisation experiment.

{SELECTION_RULES}
These rules describe selection BEFORE the reviewer claims a task. This preparation pass receives
an already selected and started task: In Progress on that exact task is expected and does not make
it ineligible. Do not walk the pool or select another experiment. Revalidate its monetisation,
age, segment, iteration, and fresh maturity evidence; fail if any gate changed. Reuse this run's
verified fresh calculation; recalculate only if freshness cannot be established.

Execution for the selected experiment:
1. Work only on the exact experiment, page, iteration, and Results task supplied by the reviewer.
   The reviewer has already verified eligibility and started that task. Re-fetch and verify its
   In Progress status, bot assignee, and Start date. If the start cannot be verified, fail without
   Jira writes. Never substitute another candidate.
{WORKER_JIRA_POLICY}
2. State the selected title, id, end timestamp, clients, segments, target project page, iteration,
   configured table prefix, and affected package-managed tables.
3. Use the `ug-experiment-calculator` skill and the installed repository `.venv` library directly;
   do not use the calculator HTTP API in this job. First run the repository freshness preflight and
   perform the skill's mandatory installed-commit versus git `main` check. If the installed
   `ug-experiment-calculator` is stale, update it through the repository's supported
   internal-library update flow before calculating. Then run the synchronous in-process
   `calculate_exp_info(exp_id, config=cfg, update_rollout=True)` with the standard
   `ExperimentCalculatorConfig.from_env()` configuration and the
   `ug_monetization_sloperator_` table prefix. This direct calculation is explicitly authorised for
   this scheduled job, including its documented writes and subscription-source refresh. During
   selection, run this procedure once for each preliminary candidate in rule 7 until one passes the
   maturity gate; do not calculate the selected experiment a second time when its verified
   calculation is still fresh. Wait for each call to finish and verify fresh successful rows in
   every expected result/stat/funnel and raw users table before continuing. Do not silently use
   stale results. If a direct calculation fails or times out, stop without publishing partial
   итогов and report the exact failure.
3. Run the complete Results → Insights → Decision / Next steps pipeline in this exact order:
   a. Generate the full publishable Results/Итоги block through `ug-experiment-calculator`,
      including its required finished-experiment Forecast/audience workflow and maturity checks.
      Insert it into the correct experiment iteration on the existing project page with Decision,
      Next steps, and Insights initially empty.
   b. Use `experiment-insight-research` to perform the full evidence-backed insight research.
      Insert the insight summary and supported question blocks into that same Results block.
   c. Only after insights are present, write concise Decision and Next steps grounded in them and
      insert them into the same block.
   Do not create a separate Confluence child page. Preserve unrelated page content and other
   iterations. Confluence content must be English.
4. Fetch the project page again after the write. Verify that the correct experiment id/iteration
   contains non-empty Results, Insights, Decision, and Next steps, that storage XML is valid, and
   that no unrelated block disappeared. If verification fails, report failure and do not announce
   success.
   Never put local/server paths or links to logs, SQL, scripts, CSVs, ZIPs, or other run artifacts
   into the project-page body. Package useful reader-safe analysis artifacts into one bundle and
   upload it as an attachment to the existing project page instead. Verify the attachment upload.
5. Leave every Jira write and every user-facing comment to the reviewer. Return a private handoff
   only; do not send Slack yourself.

Preparation result:
- Return exactly `FINALIZATION_PREPARED: <experiment id> | <project page URL> | Iteration <n>` after
  Confluence verification. Return `{NO_OP_NOTIFICATION}` for no eligible candidates, or a concise
  `Experiment finalisation failed: <reason>` line on failure. Do not return any other text.

Final notification (reviewer only):
- Return the final notification to Sloperator only from the independent reviewer; it will publish it as one top-level message in the
  configured production channel and attach this same agent session to the resulting Slack thread.
- Do not send any kickoff, progress, validation, QA, waiting, or completion-soon messages through
  Slack tools. In particular, never post messages such as "starting the daily finalisation" or
  "Validating: running a QA check". Produce exactly one Slack-facing notification, and only after
  the calculation, Confluence publication and verification, and Jira comment verification have
  all completed. Return that notification solely as the final response; do not send it yourself.
- Return the notification only in your final response; do not send it yourself through Slack tools.
- Never prefix the notification with artifact, design-review, validation, publication, verification,
  or other operational commentary. Sloperator rejects text outside the formats below.
- Do not return `SLOPERATOR_ARTIFACT` and do not attach analysis artifacts to Slack. The analysis
  bundle belongs only on the project page as described above.
- Start with exactly one compact heading sentence. Render it on one line in this shape:
  `[<project/experiment title>](<project page URL>) — experiment
  [<id>](https://www.ultimate-guitar.com/components/ab/experiment/view?id=<id>),
  Iteration <n>. Results calculated and published.`
  Put the project-page link into the title and the UG admin link into the experiment id. Do not
  print raw URLs.
- Add at most two extremely short bullets with the most important conclusions.
- Mention every distinct person listed in the project-page header table under DRI / Project owner
  and Team. Resolve Slack user ids and use real `<@USERID>` mentions; never invent ids. If a person
  cannot be resolved, name them plainly and report the resolution gap.
- Do not include a separate Project page line, Jira link/key/epic, Execution audit, calculation
  metadata, verification details, artifact list, file paths, or any other operational appendix.
  After the heading, mentions, and at most two conclusion bullets, stop.
- For every no-eligible-candidate path, return exactly `{NO_OP_NOTIFICATION}` as the entire
  response: one sentence, no bullets, candidate list, filter details, audit, explanation, or
  appendix. For a failed workflow, start with exactly `Experiment finalisation failed:` and give
  only the concise operator-facing failure. Do not put any text before these formats.

Use the current date/time in Asia/Nicosia for all relative-date and completion decisions.
Never finalise more than one experiment in this run.
"""

FINAL_NOTIFICATION_POLICY = FINALIZATION_PROMPT.split("Final notification (reviewer only):", 1)[1]

START_PROMPT = f"""[claude]
This is the authorised reviewer start pass for one UG experiment finalisation.
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{AUTOMATED_RESPONSE_STYLE}
{REVIEWER_OWNERSHIP_POLICY}

Select exactly one experiment using all of these gates before any Jira or Confluence write:
{SELECTION_RULES}

Fresh calculation procedure used to establish candidate maturity:
3. Use the `ug-experiment-calculator` skill and the installed repository `.venv` library directly;
   do not use the calculator HTTP API in this job. First run the repository freshness preflight and
   perform the skill's mandatory installed-commit versus git `main` check. If the installed
   `ug-experiment-calculator` is stale, update it through the repository's supported
   internal-library update flow before calculating. Then run the synchronous in-process
   `calculate_exp_info(exp_id, config=cfg, update_rollout=True)` with the standard
   `ExperimentCalculatorConfig.from_env()` configuration and the
   `ug_monetization_sloperator_` table prefix. This direct calculation is explicitly authorised for
   this scheduled job, including its documented writes and subscription-source refresh. During
   selection, run this procedure once for each preliminary candidate in rule 7 until one passes the
   maturity gate; do not calculate the selected experiment a second time when its verified
   calculation is still fresh. Wait for each call to finish and verify fresh successful rows in
   every expected result/stat/funnel and raw users table before continuing. Do not silently use
   stale results. If a direct calculation fails or times out, stop without publishing partial
   итогов and report the exact failure.

Only after a candidate passes every gate, start its exact Results task:
{REVIEWER_START_POLICY}
Do not generate or publish Results, Insights, Decision, or Next steps in this pass.
Return exactly one line after the verified start:
FINALIZATION_STARTED: <experiment id> | <project page URL> | Iteration <n> | <Results task key>
Return exactly {NO_OP_NOTIFICATION} if none qualify. On failure return exactly
Experiment finalisation failed: <reason>.
"""


REVIEW_PROMPT = f"""\
[claude]
This is the authorised independent review pass for one prepared UG experiment finalisation.
{AUTOMATED_RESPONSE_STYLE}
{AUTOMATED_SESSION_REPOSITORY_POLICY}
{AUTOMATED_ATLASSIAN_IDENTITY}
{REVIEWER_OWNERSHIP_POLICY}

Review only the exact experiment, project page, and iteration supplied below. Re-fetch the page and
verify Results, Insights, Decision, and Next steps are complete, valid, and belong to that iteration.
Use the repository Jira helper with `--as-bot` for every Jira command. Resolve the service account from
`/myself` (`712020:e603f3a9-4b70-4ed8-866f-280460a661c5`). Re-fetch the exact Results task's current
status and available transitions before any status change:
- From `Backlog` or `To Do`, the intermediate transition ID `281` to `In Progress` is explicitly
  authorised for recovery of a missing preparation start. Execute it and re-fetch to verify
  `In Progress` before attempting `181`. Do not ask a human to make this authorised transition.
- From `In Progress`, use transition ID `181` to `In Review` after the completion writes below.
- If already `In Review` or `Done`, do not move it backwards or repeat a transition. Verify the
  existing publication and avoid duplicate comments; preserve a completed task's fields.
- For another status or an unavailable required transition, stop with a concise failure stating
  the observed status and missing transition. Do not infer a permissions problem from HTTP 400.
For a task still awaiting completion, assign it to the service account, preserve an existing
`Start date` (`customfield_10312`) or fill it with today's `YYYY-MM-DD` date if missing, and set
`Due date` (`duedate`) to today. Reuse an existing matching English publication comment instead
of adding a duplicate; otherwise add the short publication comment. Re-fetch and verify the
fields and comment, perform `181` only from verified `In Progress`, and re-fetch to verify
`In Review`. Return the final Slack notification in the exact production format below.
{FINAL_NOTIFICATION_POLICY} Do not send Slack yourself. On any failure return exactly `Experiment finalisation failed: <reason>`.

Preparation result:
{{prepared_result}}
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


class InvalidFinalizationNotification(ValueError):
    """The scheduled agent returned text that is unsafe to publish directly."""


PREPARED_RE = re.compile(r"FINALIZATION_PREPARED:\s*(\d+)\s*\|\s*(\S+)\s*\|\s*Iteration\s+(\d+)")


STARTED_RE = re.compile(
    r"FINALIZATION_STARTED: (\d+) \| (https://[^\s|]+) \| Iteration (\d+) \| (UMN-\d+)"
)


def is_start_result(text: str) -> bool:
    return bool(STARTED_RE.fullmatch(text.strip())) or text.strip().startswith(
        (NO_OP_PREFIX, *FAILURE_PREFIXES)
    )


def is_reviewer_result(text: str) -> bool:
    return is_start_result(text) or is_finalization_notification(text)


def is_preparation_result(text: str) -> bool:
    stripped = text.strip()
    return bool(PREPARED_RE.search(stripped) or stripped.startswith(NO_OP_PREFIX) or stripped.startswith(FAILURE_PREFIXES))


def normalize_finalization_notification(text: str) -> str:
    """Remove model preamble and enforce one of the production notification shapes."""
    stripped = text.strip()
    lines = stripped.splitlines()
    for index, line in enumerate(lines):
        candidate = line.strip()
        if (
            candidate.startswith(("[", "<http"))
            and " — experiment " in candidate
            and "components/ab/experiment/view?id=" in candidate
            and "Results calculated and published." in candidate
        ):
            return "\n".join(lines[index:]).strip()
    if stripped.startswith(NO_OP_PREFIX):
        return NO_OP_NOTIFICATION
    if stripped.startswith(FAILURE_PREFIXES):
        return stripped
    raise InvalidFinalizationNotification(
        "Scheduled agent response has no valid completion, no-op, or failure heading"
    )


def is_finalization_notification(text: str) -> bool:
    """Return whether a headless result is publishable rather than an interim update."""
    try:
        normalize_finalization_notification(text)
    except InvalidFinalizationNotification:
        return False
    return True


def next_run_at(
    now: dt.datetime,
    timezone_name: str = "Asia/Nicosia",
    hour: int = 12,
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


async def run_once(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
) -> str:
    """Run headlessly, publish once, and attach the resumable session."""
    start_run = await agent.execute_once(
        START_PROMPT,
        settings.experiment_finalizer_timeout_seconds,
        job_name="experiment-finalizer-reviewer",
        accept_result=is_start_result,
    )
    if start_run.text.strip().startswith(NO_OP_PREFIX):
        return await publish_run(client, agent, settings, replace(start_run, text=NO_OP_NOTIFICATION))
    if STARTED_RE.fullmatch(start_run.text.strip()) is None:
        raise InvalidFinalizationNotification(start_run.text)
    return await run_preparation(client, agent, settings, start_run)


async def run_preparation(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    started_run: HeadlessAgentRun,
) -> str:
    prepared_run = await agent.execute_once(
        FINALIZATION_PROMPT + "\n\nReviewer verified start:\n" + started_run.text,
        settings.experiment_finalizer_timeout_seconds,
        job_name="experiment-finalizer-preparer",
        accept_result=is_preparation_result,
    )
    prepared_text = prepared_run.text.strip()
    if prepared_text.startswith(NO_OP_PREFIX) or prepared_text.startswith(FAILURE_PREFIXES):
        raise InvalidFinalizationNotification(prepared_text)
    if PREPARED_RE.search(prepared_text) is None:
        raise InvalidFinalizationNotification("Preparation agent returned no FINALIZATION_PREPARED marker")
    prepared = PREPARED_RE.fullmatch(prepared_text)
    started = STARTED_RE.fullmatch(started_run.text.strip())
    if prepared is None or started is None or prepared.groups() != started.groups()[:3]:
        raise InvalidFinalizationNotification("Preparation does not match reviewer selection")
    return await run_review(
        client, agent, settings, prepared_text,
        reviewer_session_id=started_run.session_id, start_context=started_run.text,
    )


async def run_review(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    prepared_text: str,
    reviewer_session_id: str | None = None,
    start_context: str = "",
) -> str:
    if PREPARED_RE.fullmatch(prepared_text.strip()) is None:
        raise InvalidFinalizationNotification("Invalid preparation result")
    review_run = await agent.execute_once(
        REVIEW_PROMPT.format(prepared_result=prepared_text + "\n" + start_context),
        settings.experiment_finalizer_timeout_seconds,
        job_name="experiment-finalizer-reviewer",
        accept_result=is_finalization_notification,
        existing_session_id=reviewer_session_id,
    )
    return await publish_run(client, agent, settings, review_run)


async def publish_run(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    run: HeadlessAgentRun,
) -> str:
    """Publish and attach a completed or restart-recovered finalizer run."""
    notification = normalize_finalization_notification(run.text)
    published_run = replace(run, text=notification)
    response = await client.chat_postMessage(
        channel=settings.experiment_finalizer_channel,
        markdown_text=notification,
        unfurl_links=False,
        unfurl_media=False,
    )
    channel_id = response.get("channel", settings.experiment_finalizer_channel)
    await agent.attach_session(
        channel_id,
        response["ts"],
        published_run,
    )
    return notification


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
            settings.experiment_finalizer_timezone,
            settings.experiment_finalizer_hour,
        )
        delay = (target.astimezone(dt.UTC) - now).total_seconds()
        LOGGER.info("Next experiment finalizer run scheduled for %s", target.isoformat())
        await asyncio.sleep(delay)
        if not enabled():
            LOGGER.info("Scheduled experiment finalizer run disabled from admin")
            continue
        run_task: asyncio.Task[str] | None = None
        try:
            LOGGER.info("Starting scheduled experiment finalizer run")
            run_task = asyncio.create_task(
                run_once(client, agent, settings),
                name="scheduled-experiment-finalizer-run",
            )
            await run_task
            LOGGER.info("Experiment finalizer run completed")
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                if run_task is not None:
                    run_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await run_task
                raise
            LOGGER.info("Scheduled experiment finalizer run cancelled from admin")
        except Exception:
            LOGGER.exception("Could not start the daily experiment finalizer")


async def cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel a scheduler task during application shutdown."""
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
