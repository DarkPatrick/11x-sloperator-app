"""Daily autonomous finalisation of one recent monetisation experiment."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import suppress
from typing import Protocol
from zoneinfo import ZoneInfo

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import HeadlessAgentRun
from sloperator.config import Settings

LOGGER = logging.getLogger(__name__)

FINALIZATION_PROMPT = """\
This is the authorised daily autonomous experiment-finalisation job. Complete the whole
workflow in this single turn. The user explicitly pre-approves progression through all three
publication stages (Results, then Insights, then Decision / Next steps), including the required
Confluence update and Jira comment. Do not pause to request approval between stages. This
instruction intentionally overrides only the interactive approval pauses in the skills; keep
all their data-quality, maturity, verification, language, and publication safeguards.

Goal: finalise exactly one eligible UG monetisation experiment.

Selection rules:
1. Use the UG experiment admin/source of truth to find experiments whose actual experiment end
   timestamp is in the closed interval [now minus one calendar month, now]. Include only
   experiments that have already ended; exclude running, scheduled, paused without an actual
   end, or otherwise unfinished experiments.
2. Keep only experiments that have at least one configured segment in the admin.
3. Locate each experiment's project page and exclude it if the final Results/Итоги section for
   this experiment/iteration is already populated. Do not mistake a template, empty placeholder,
   design table, or results for another iteration for completed итогов.
4. From the remaining candidates choose exactly the oldest by actual end timestamp (then by
   experiment id as a deterministic tie-breaker). Re-check all eligibility conditions immediately
   before making any write. If no eligible experiment exists, make no Confluence or Jira writes
   and return a short no-op report with the filters checked.

Execution for the selected experiment:
1. State the selected title, id, end timestamp, clients, segments, target project page, iteration,
   configured table prefix, and affected package-managed tables.
2. Use the `ug-experiment-calculator` skill and the installed repository `.venv` library directly;
   do not use the calculator HTTP API in this job. First run the repository freshness preflight and
   perform the skill's mandatory installed-commit versus git `main` check. If the installed
   `ug-experiment-calculator` is stale, update it through the repository's supported
   internal-library update flow before calculating. Then run the synchronous in-process
   `calculate_exp_info(exp_id, config=cfg, update_rollout=True)` with the standard
   `ExperimentCalculatorConfig.from_env()` configuration and the
   `ug_monetization_sloperator_` table prefix. This direct calculation is explicitly authorised for
   this scheduled job, including its documented writes and subscription-source refresh. Wait for
   the call to finish and verify fresh successful rows in every expected result/stat/funnel and raw
   users table before continuing. Do not silently use stale results. If the direct calculation
   fails or times out, stop without publishing partial итогов and report the exact failure.
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
5. Resolve the project's Jira epic, then the Results/Итоги task for the matching iteration. Use the
   repository Jira helper and add one short English comment saying the results were calculated and
   published automatically, with the experiment id and project-page link. Re-fetch the issue and
   verify the comment. Do not comment on a guessed epic, a different iteration, or a generic task;
   if the exact task cannot be established, report that as an incomplete step.

Notification:
- Return the final notification to Sloperator; it will publish it as one top-level message in
  `ug-monetization-pvt` and attach this same agent session to the resulting Slack thread.
- Do not send any kickoff, progress, validation, QA, waiting, or completion-soon messages through
  Slack tools. In particular, never post messages such as "starting the daily finalisation" or
  "Validating: running a QA check". Produce exactly one Slack-facing notification, and only after
  the calculation, Confluence publication and verification, and Jira comment verification have
  all completed. Return that notification solely as the final response; do not send it yourself.
- Return the notification only in your final response; do not send it yourself through Slack tools.
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

Use the current date/time in Asia/Nicosia for all relative-date and completion decisions.
Never finalise more than one experiment in this run.
"""


class AgentSubmitter(Protocol):
    async def execute_once(self, text: str, timeout_seconds: int) -> HeadlessAgentRun: ...

    async def attach_session(
        self,
        channel_id: str,
        thread_ts: str,
        run: HeadlessAgentRun,
    ) -> None: ...


def next_run_at(
    now: dt.datetime,
    timezone_name: str = "Asia/Nicosia",
    hour: int = 12,
) -> dt.datetime:
    """Return the next daily wall-clock run time, preserving Cyprus DST."""
    timezone = ZoneInfo(timezone_name)
    local_now = now.astimezone(timezone)
    candidate = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += dt.timedelta(days=1)
    return candidate


async def run_once(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
) -> str:
    """Run headlessly, publish once, and attach the resumable session."""
    run = await agent.execute_once(
        FINALIZATION_PROMPT,
        settings.experiment_finalizer_timeout_seconds,
    )
    response = await client.chat_postMessage(
        channel=settings.experiment_finalizer_channel,
        markdown_text=run.text,
        unfurl_links=False,
        unfurl_media=False,
    )
    await agent.attach_session(
        settings.experiment_finalizer_channel,
        response["ts"],
        run,
    )
    return run.text


async def run_daily(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
) -> None:
    """Run forever at the configured local wall-clock hour."""
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
        try:
            result = await run_once(client, agent, settings)
            LOGGER.info("Experiment finalizer submission result: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Could not start the daily experiment finalizer")


async def cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel a scheduler task during application shutdown."""
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
