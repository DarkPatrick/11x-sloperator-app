"""Daily autonomous finalisation of one recent monetisation experiment."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import suppress
from typing import Protocol
from zoneinfo import ZoneInfo

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import SubmitResult
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
2. Use the `ug-experiment-calculator` skill. Enqueue a fresh calculation through the calculator
   HTTP API (`POST /calculate?exp_id=<id>`), retain the request_id, and poll `/status` every
   30 seconds until a terminal state. Continue only after `succeeded`. Do not silently use stale
   results. If the API fails or times out, stop without publishing partial итогов; do not use the
   direct-library mutation fallback in this unattended job.
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
5. Resolve the project's Jira epic, then the Results/Итоги task for the matching iteration. Use the
   repository Jira helper and add one short English comment saying the results were calculated and
   published automatically, with the experiment id and project-page link. Re-fetch the issue and
   verify the comment. Do not comment on a guessed epic, a different iteration, or a generic task;
   if the exact task cannot be established, report that as an incomplete step.

Notification (temporary test routing):
- Do not post to `ug-monetization-pvt` in this test configuration.
- Return the notification in your final response; Sloperator will deliver it only to the owner's
  Slack DM.
- Start with the experiment title and id and say that its results were calculated and published.
- Add at most two extremely short bullets with the most important conclusions.
- Mention every distinct person listed in the project-page header table under DRI / Project owner
  and Team. Resolve Slack user ids and use real `<@USERID>` mentions; never invent ids. If a person
  cannot be resolved, name them plainly and report the resolution gap.
- Include the project-page link and the Jira issue key/link. Keep operational detail out of this
  notification, but append a compact execution audit after it: calculator request_id/status,
  eligibility evidence, maturity result, Confluence verification, and Jira verification.

Use the current date/time in Asia/Nicosia for all relative-date and completion decisions.
Never finalise more than one experiment in this run.
"""


class AgentSubmitter(Protocol):
    async def submit(
        self,
        client: AsyncWebClient,
        *,
        channel_id: str,
        message_ts: str,
        thread_ts: str,
        text: str,
        show_status: bool = True,
        timeout_seconds: int | None = None,
    ) -> SubmitResult: ...


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
) -> SubmitResult:
    """Create a private audit thread and enqueue the autonomous agent turn."""
    conversation = await client.conversations_open(users=settings.slack_user_id)
    channel_id = conversation["channel"]["id"]
    kickoff = await client.chat_postMessage(
        channel=channel_id,
        text=(
            "Запускаю ежедневную финализацию одного монетизационного эксперимента "
            "(тестовый режим: результат только в этой личке)."
        ),
    )
    message_ts = kickoff["ts"]
    return await agent.submit(
        client,
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=message_ts,
        text=FINALIZATION_PROMPT,
        show_status=True,
        timeout_seconds=settings.experiment_finalizer_timeout_seconds,
    )


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
