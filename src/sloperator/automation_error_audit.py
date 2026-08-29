"""Daily read-only Claude audit of failed Sloperator automations."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import HeadlessAgentRun
from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.config import Settings

LOGGER = logging.getLogger(__name__)
TIMEZONE = "Asia/Nicosia"
HOUR = 14
TIMEOUT_SECONDS = 3_600
NO_ERRORS = "NO_RECENT_AUTOMATION_ERRORS"
REPORT_PREFIX = "Automation errors detected:"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

AUDIT_PROMPT = f"""\
[claude:opus]
This is the daily read-only audit of Sloperator automation failures. Work inside the current
Sloperator repository and inspect executions from the preceding 24 hours. Check both cron jobs
managed by this host and Slack-triggered agent runs. Use the Sloperator SQLite history, current
systemd journal, cron definitions, and their referenced logs as read-only evidence. Distinguish a
real terminal failure from an active run, a recovered run, and stale append-only stderr left by an
older execution. Ignore this audit's own currently-running record.

The `sloperator-vpn` container being stopped, exited, unavailable, or awaiting an interactive VPN
login is an expected operational state, not an automation failure. Do not report it, its restart
policy, VPN authentication expiry, unavailable VPN proxy, or diagnostics disabled only because
that container is down. Report a separate automation only if that automation itself actually
failed; do not infer failure merely from VPN unavailability.

STRICT SAFETY BOUNDARY: investigate and report only. Do not edit or create files, change the
database, restart or signal services, alter cron/configuration, send Slack messages, invoke fixes,
commit, or push. Do not repair anything even when the remedy looks obvious.

{AUTOMATED_RESPONSE_STYLE}

Output contract:
- If there were no actual failures in the preceding 24 hours, return exactly `{NO_ERRORS}`.
- If failures exist, begin exactly with `{REPORT_PREFIX}` and write a concise Russian report for
  the owner: what failed, the verified cause (or explicitly that it is not established), concrete
  impact, confidence, and the recommended next action. Separate facts from hypotheses.
- Return only the final report to Sloperator. Do not send it through Slack yourself.
"""


class AgentSubmitter(Protocol):
    async def execute_once(
        self,
        text: str,
        timeout_seconds: int,
        *,
        job_name: str = "scheduled-agent",
        workspace: Path | None = None,
        accept_result: Callable[[str], bool] = lambda _: True,
        max_interim_results: int = 2,
    ) -> HeadlessAgentRun: ...


def is_final_result(text: str) -> bool:
    stripped = text.strip()
    return stripped == NO_ERRORS or stripped.startswith(REPORT_PREFIX)


def next_run_at(now: dt.datetime) -> dt.datetime:
    """Return the next 14:00 Cyprus wall-clock time, including weekends and DST."""
    local_now = now.astimezone(ZoneInfo(TIMEZONE))
    candidate = local_now.replace(hour=HOUR, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += dt.timedelta(days=1)
    return candidate


async def run_once(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
) -> str | None:
    """Run the audit and DM its report only when failures were found."""
    run = await agent.execute_once(
        AUDIT_PROMPT,
        TIMEOUT_SECONDS,
        job_name="automation-error-audit",
        workspace=PROJECT_ROOT,
        accept_result=is_final_result,
    )
    return await publish_run(client, settings, run)


async def publish_run(
    client: AsyncWebClient,
    settings: Settings,
    run: HeadlessAgentRun,
) -> str | None:
    """Publish a new or restart-recovered result if it contains failures."""
    report = run.text.strip()
    if report == NO_ERRORS:
        return None
    if not report.startswith(REPORT_PREFIX):
        raise ValueError("Automation audit returned an invalid final response")
    conversation = await client.conversations_open(users=settings.slack_user_id)
    await client.chat_postMessage(
        channel=conversation["channel"]["id"],
        markdown_text=report,
        unfurl_links=False,
        unfurl_media=False,
    )
    return report


async def run_daily(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    enabled: Callable[[], bool] = lambda: True,
) -> None:
    """Run forever once per calendar day at 14:00 Cyprus time."""
    while True:
        now = dt.datetime.now(dt.UTC)
        target = next_run_at(now)
        LOGGER.info("Next automation error audit scheduled for %s", target.isoformat())
        await asyncio.sleep((target.astimezone(dt.UTC) - now).total_seconds())
        if not enabled():
            LOGGER.info("Scheduled automation error audit disabled from admin")
            continue
        try:
            LOGGER.info("Starting scheduled automation error audit")
            await run_once(client, agent, settings)
            LOGGER.info("Automation error audit completed")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Daily automation error audit failed")


async def cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
