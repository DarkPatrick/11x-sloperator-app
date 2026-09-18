"""Rebuild the daily red-alert Metabase dashboard from the monitoring channel reports.

The `metabase-anomaly-detector` posts three separate reports into
`#ug-monetization-metrics-monitoring` a few minutes apart. This trigger waits until all three
of a run have arrived, hands their verbatim text to the `ug-ai-analyst` alert-dashboard
pipeline, and announces the rebuilt dashboard in the same channel.

The pipeline itself is deterministic (four repository scripts), so no agent is involved: the
reports are parsed for their :red_circle: lines, one chart plus two KPI tiles are generated per
red alert, and the dashboard is rebuilt from scratch.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.config import Settings
from sloperator.store import EventStore

LOGGER = logging.getLogger(__name__)

WEB_REPORT_MARKER = "UG Monetisation: WEB health monitoring"
MOBILE_REPORT_MARKER = "UG Monetisation: Mobile Health Monitoring Dashboard"
SUBSCRIPTIONS_REPORT_MARKER = "UG Monetisation: WEB Subscriptions Monitoring"
REQUIRED_MARKERS = (WEB_REPORT_MARKER, MOBILE_REPORT_MARKER, SUBSCRIPTIONS_REPORT_MARKER)

# The three reports of one run land within ~10 minutes of each other; anything further apart
# belongs to a different run and must not complete a set.
RUN_WINDOW_SECONDS = 3_600
# A report is split into continuation chunks posted by the same webhook within seconds.
CHUNK_WINDOW_SECONDS = 60
# Slack delivers the header before the webhook has finished posting the continuation chunks.
CHUNK_SETTLE_SECONDS = 5
HISTORY_LIMIT = 200

RED_ALERT_RE = re.compile(
    r"^(?::red_circle:|🔴)\s*(?P<name>.+?):\s*(?P<value>[-\d][\d,.]*|\?)\s*\((?P<detail>.*)\)\s*$"
)
RUN_RE = re.compile(r"\|\s*Run:\s*(?P<run>\d{4}-\d{2}-\d{2}) \d{2}:\d{2}")


class AlertDashboardError(RuntimeError):
    """The alert-dashboard pipeline failed."""


@dataclass(frozen=True, slots=True)
class Report:
    """One monitoring report: its header message plus the continuation chunks below it."""

    marker: str
    ts: str
    text: str


@dataclass(frozen=True, slots=True)
class PublishedDashboard:
    """What the publish step wrote about the dashboard it has just rebuilt."""

    url: str
    tabs: tuple[str, ...]
    blocks: int


def is_top_level(message: dict[str, Any]) -> bool:
    """Treat a thread parent as top-level: Slack stamps `thread_ts` on it once it has replies.

    The web- and mobile-health triggers answer these same reports, so by the time the run is
    assembled the reports they replied to already carry `thread_ts == ts`.
    """
    thread_ts = message.get("thread_ts")
    return not isinstance(thread_ts, str) or thread_ts == message.get("ts")


def report_marker(text: str) -> str | None:
    """Return the report a top-level message starts, or None when it is not a header."""
    return next((marker for marker in REQUIRED_MARKERS if marker in text), None)


def is_alert_dashboard_trigger(event: dict[str, Any], settings: Settings) -> bool:
    """Match a top-level monitoring report from the configured detector webhook."""
    text = event.get("text")
    return (
        event.get("channel") == settings.mobile_health_alert_channel
        and event.get("bot_id") == settings.mobile_health_bot_id
        and is_top_level(event)
        and isinstance(text, str)
        and report_marker(text) is not None
    )


def count_red_alerts(text: str) -> int:
    """Count :red_circle: metric lines, using the detector's own report grammar."""
    return sum(1 for line in text.splitlines() if RED_ALERT_RE.match(line.strip()))


def run_date(text: str) -> str | None:
    """Return the `Run: <date>` of the first report header in the assembled dump."""
    match = RUN_RE.search(text)
    return match.group("run") if match is not None else None


def collect_reports(messages: Sequence[tuple[str, str]]) -> dict[str, Report]:
    """Assemble each report from its header message and the chunks that follow it.

    ``messages`` are ``(ts, text)`` pairs from the detector webhook in any order. Only the most
    recent occurrence of each report is kept, so a rerun supersedes the earlier run.
    """
    ordered = sorted(messages, key=lambda item: float(item[0]))
    reports: dict[str, Report] = {}
    current: str | None = None
    chunks: dict[str, list[str]] = {}
    for timestamp, text in ordered:
        marker = report_marker(text)
        if marker is not None:
            reports[marker] = Report(marker, timestamp, text)
            chunks[marker] = []
            current = marker
            continue
        if current is None:
            continue
        header = reports[current]
        if float(timestamp) - float(header.ts) > CHUNK_WINDOW_SECONDS:
            current = None
            continue
        chunks[current].append(text)
    return {
        marker: Report(marker, report.ts, "\n".join([report.text, *chunks.get(marker, [])]))
        for marker, report in reports.items()
    }


def complete_run(reports: dict[str, Report]) -> dict[str, Report] | None:
    """Return the three reports when they all belong to one run, else None."""
    if any(marker not in reports for marker in REQUIRED_MARKERS):
        return None
    selected = {marker: reports[marker] for marker in REQUIRED_MARKERS}
    stamps = [float(report.ts) for report in selected.values()]
    if max(stamps) - min(stamps) > RUN_WINDOW_SECONDS:
        return None
    return selected


def build_dump(reports: dict[str, Report]) -> str:
    """Concatenate the run's reports verbatim, in the order the channel posted them."""
    ordered = sorted(reports.values(), key=lambda report: float(report.ts))
    return "\n\n".join(report.text for report in ordered) + "\n"


def announcement(published: PublishedDashboard, date: str | None) -> str:
    """Build the one-line channel message announcing the rebuilt dashboard."""
    day = f" за {date}" if date else ""
    return f":bar_chart: <{published.url}|Свежий дашборд>{day} по аномалиям собран"


class AlertDashboardResponder:
    """Wait for a full report run, rebuild the dashboard, and announce it in the channel."""

    TRIGGER_KEY = "alert-dashboard"

    def __init__(self, settings: Settings, store: EventStore | None = None) -> None:
        self.settings = settings
        self.store = store
        self.workspace = settings.agent_workspace.resolve()
        self.state_path = settings.database_path.parent / "alert-dashboard-state.json"
        self._in_flight: set[str] = set()

    async def _record(self, message_ts: str, status: str, detail: str | None = None) -> None:
        """Report this run to the admin trigger calendar.

        This trigger launches no agent, so it leaves no `agent_requests` row and would be
        invisible in the admin UI otherwise. Reporting must never break a rebuild that is
        otherwise fine, so a bookkeeping failure is logged and swallowed.
        """
        if self.store is None:
            return
        try:
            await asyncio.to_thread(
                self.store.record_pipeline_trigger_run,
                self.TRIGGER_KEY,
                self.settings.mobile_health_alert_channel,
                message_ts,
                status,
                detail,
            )
        except Exception:                                  # noqa: BLE001 — never fail the run
            LOGGER.exception("Could not record the alert dashboard run %s", message_ts)

    async def handle(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        message_ts = event.get("ts")
        if not isinstance(message_ts, str) or message_ts in self._in_flight:
            return
        self._in_flight.add(message_ts)
        try:
            await asyncio.sleep(CHUNK_SETTLE_SECONDS)
            reports = complete_run(await self._collect_run(client))
            if reports is None:
                LOGGER.info(
                    "Alert dashboard run is not complete yet at message %s", message_ts
                )
                return
            run_ts = max(report.ts for report in reports.values())
            if run_ts != message_ts:
                LOGGER.debug(
                    "Message %s is not the last report of its run (%s)", message_ts, run_ts
                )
                return
            if await asyncio.to_thread(self._already_built, run_ts):
                LOGGER.info("Alert dashboard for run %s was already rebuilt", run_ts)
                return
            await self._record(run_ts, "running")
            await self._rebuild_and_announce(client, reports, run_ts)
            await self._record(run_ts, "completed")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.exception("Alert dashboard rebuild failed for message %s", message_ts)
            await self._record(message_ts, "failed", str(error)[-1_000:])
            await self._notify_owner(client, str(error))
        finally:
            self._in_flight.discard(message_ts)

    async def _rebuild_and_announce(
        self,
        client: AsyncWebClient,
        reports: dict[str, Report],
        run_ts: str,
    ) -> None:
        dump = build_dump(reports)
        red_alerts = count_red_alerts(dump)
        date = run_date(dump) or dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
        LOGGER.info("Rebuilding the alert dashboard for run %s: %d red alert(s)", date, red_alerts)
        published = await self.rebuild(dump, date)
        await asyncio.to_thread(self._record_built, run_ts, date, published, red_alerts)
        await client.chat_postMessage(
            channel=self.settings.mobile_health_alert_channel,
            text=announcement(published, date),
            unfurl_links=False,
            unfurl_media=False,
        )

    async def rebuild(self, dump: str, date: str) -> PublishedDashboard:
        """Run the repository pipeline over the report dump and return what it published."""
        python = self.workspace / ".venv" / "bin" / "python"
        scripts = self.workspace / "scripts"
        if not python.is_file():
            raise AlertDashboardError(
                f"Repository virtualenv Python is missing in {self.workspace}"
            )
        for name in (
            "alert_dashboard_registry.py",
            "alert_dashboard_parse_report.py",
            "alert_dashboard_build_sql.py",
            "alert_dashboard_publish.py",
        ):
            if not (scripts / name).is_file():
                raise AlertDashboardError(f"Pipeline script {name} is missing in {scripts}")
        detector_src = self.settings.alert_dashboard_detector_src
        if not detector_src.is_dir():
            raise AlertDashboardError(f"Detector source directory is missing: {detector_src}")

        stamp = date.replace("-", "")
        out_dir = self.workspace / "output" / "alert_dashboard"
        out_dir.mkdir(parents=True, exist_ok=True)
        dump_path = out_dir / f"{stamp}_channel_dump.txt"
        await asyncio.to_thread(dump_path.write_text, dump)
        alerts = f"output/alert_dashboard/{stamp}_alerts.json"
        cards = f"output/alert_dashboard/{stamp}_alert_cards.json"
        published = f"output/alert_dashboard/{stamp}_published_dashboard.json"

        # The registry is derived from the live source dashboards and is not committed, so it is
        # rebuilt on every run rather than assumed to exist from an earlier one.
        await self._run(
            (str(python), str(scripts / "alert_dashboard_registry.py"),
             "--detector-src", str(detector_src))
        )
        await self._run(
            (str(python), str(scripts / "alert_dashboard_parse_report.py"),
             str(dump_path), "--red-only", "--out", alerts)
        )
        await self._run(
            (str(python), str(scripts / "alert_dashboard_build_sql.py"),
             "--alerts", alerts, "--red-only", "--validate", "--out", cards)
        )
        await self._run(
            (str(python), str(scripts / "alert_dashboard_publish.py"),
             "--cards", cards,
             "--dashboard", str(self.settings.alert_dashboard_id),
             "--collection", str(self.settings.alert_dashboard_collection),
             "--out", published)
        )
        return await asyncio.to_thread(self._read_published, self.workspace / published)

    async def _run(self, command: Sequence[str]) -> str:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.workspace,
            env={**os.environ, "UG_SKIP_PREFLIGHT": "1"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self.settings.alert_dashboard_timeout_seconds,
            )
        except (asyncio.CancelledError, TimeoutError):
            if process.returncode is None:
                process.terminate()
                with suppress(ProcessLookupError, TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=10)
            raise
        output = stdout.decode(errors="replace").strip()
        step = Path(command[1]).name
        if process.returncode != 0:
            tail = output[-2_000:] if output else "no output"
            raise AlertDashboardError(f"{step} exited {process.returncode}: {tail}")
        LOGGER.info("Alert dashboard step %s: %s", step, output[-1_000:])
        return output

    @staticmethod
    def _read_published(path: Path) -> PublishedDashboard:
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise AlertDashboardError(f"Publish step wrote no readable summary: {error}") from error
        url = payload.get("dashboard")
        if not isinstance(url, str):
            raise AlertDashboardError("Publish summary carries no dashboard URL")
        tabs = payload.get("tabs")
        blocks = payload.get("blocks")
        return PublishedDashboard(
            url=url,
            tabs=tuple(str(tab) for tab in tabs) if isinstance(tabs, list) else (),
            blocks=len(blocks) if isinstance(blocks, list) else 0,
        )

    async def _collect_run(self, client: AsyncWebClient) -> dict[str, Report]:
        oldest = dt.datetime.now(dt.UTC).timestamp() - RUN_WINDOW_SECONDS - CHUNK_WINDOW_SECONDS
        response = await client.conversations_history(
            channel=self.settings.mobile_health_alert_channel,
            oldest=f"{oldest:.6f}",
            limit=HISTORY_LIMIT,
        )
        messages: list[tuple[str, str]] = []
        raw_messages: list[dict[str, Any]] = response.get("messages", [])
        for message in raw_messages:
            timestamp = message.get("ts")
            text = message.get("text")
            if (
                message.get("bot_id") == self.settings.mobile_health_bot_id
                and is_top_level(message)
                and isinstance(timestamp, str)
                and isinstance(text, str)
            ):
                messages.append((timestamp, text))
        return collect_reports(messages)

    def _already_built(self, run_ts: str) -> bool:
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return False
        return bool(state.get("run_ts") == run_ts)

    def _record_built(
        self,
        run_ts: str,
        date: str,
        published: PublishedDashboard,
        red_alerts: int,
    ) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {
                    "run_ts": run_ts,
                    "run_date": date,
                    "red_alerts": red_alerts,
                    "blocks": published.blocks,
                    "dashboard": published.url,
                    "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    async def _notify_owner(self, client: AsyncWebClient, detail: str) -> None:
        """Report a failed rebuild privately instead of adding noise to the channel."""
        try:
            channel = self.settings.automation_alert_channel
            if not channel:
                conversation = await client.conversations_open(users=self.settings.slack_user_id)
                channel = conversation["channel"]["id"]
            await client.chat_postMessage(
                channel=channel,
                text=(
                    ":warning: Дашборд по аномалиям не собрался — "
                    f"пайплайн упал.\n```{detail[-1_500:]}```"
                ),
            )
        except Exception:
            LOGGER.exception("Could not report the alert dashboard failure to the owner")
