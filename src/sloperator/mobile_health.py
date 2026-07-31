"""Launch durable investigations for critical mobile health drops."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.anomaly_alerts import AgentSubmitter
from sloperator.config import Settings

LOGGER = logging.getLogger(__name__)

REPORT_MARKER = "UG Monetisation Health Monitoring Dashboard"
LEGACY_REPORT_MARKER = "*APP Health Monitoring Report*"
PLATFORM_RE = re.compile(r"^:(?:robot_face|green_apple):\s+\*?(Android|iOS)\*?\s*$")
QUERY_RE = re.compile(r"^<(?P<url>https?://[^|>]+)\|(?P<title>.+)>$")
CRITICAL_DROP_RE = re.compile(
    r"^:red_circle:\s+(?P<metric>.+?)\s+\((?P<details>.*\bcritical\b.*)\)$"
)


@dataclass(frozen=True, slots=True)
class MobileCriticalMetric:
    platform: str
    query_title: str
    query_url: str
    metric_line: str
    diagnostics: tuple[str, ...] = ()


def is_mobile_health_trigger(event: dict[str, Any], settings: Settings) -> bool:
    """Match only the configured monitor's top-level mobile dashboard report."""
    text = event.get("text")
    return (
        event.get("channel") == settings.mobile_health_alert_channel
        and event.get("bot_id") == settings.mobile_health_bot_id
        and not isinstance(event.get("thread_ts"), str)
        and isinstance(text, str)
        and (REPORT_MARKER in text or LEGACY_REPORT_MARKER in text)
    )


def parse_critical_mobile_metrics(
    text: str,
    *,
    limit: int = 5,
) -> list[MobileCriticalMetric]:
    """Extract at most ``limit`` red critical metrics from Android and iOS sections."""
    selected: list[MobileCriticalMetric] = []
    platform: str | None = None
    query_title = ""
    query_url = ""
    diagnostic_target: int | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        platform_match = PLATFORM_RE.match(line)
        if platform_match is not None:
            platform = platform_match.group(1)
            query_title = ""
            query_url = ""
            diagnostic_target = None
            continue
        if line.startswith("─") or line == "Other":
            platform = None
            query_title = ""
            query_url = ""
            diagnostic_target = None
            continue
        if platform is None:
            continue
        query_match = QUERY_RE.match(line)
        if query_match is not None:
            query_title = query_match.group("title")
            query_url = query_match.group("url")
            diagnostic_target = None
            continue
        if line.startswith((":mag:", ":bar_chart:")) and diagnostic_target is not None:
            previous = selected[diagnostic_target]
            selected[diagnostic_target] = MobileCriticalMetric(
                platform=previous.platform,
                query_title=previous.query_title,
                query_url=previous.query_url,
                metric_line=previous.metric_line,
                diagnostics=(*previous.diagnostics, line),
            )
            continue
        if line.startswith((":red_circle:", ":large_green_circle:")):
            diagnostic_target = None
        if len(selected) >= limit or CRITICAL_DROP_RE.match(line) is None:
            continue
        selected.append(
            MobileCriticalMetric(
                platform=platform,
                query_title=query_title or "Unknown dashboard card",
                query_url=query_url,
                metric_line=line,
            )
        )
        diagnostic_target = len(selected) - 1
    return selected


def build_mobile_health_agent_prompt(
    report_text: str,
    metrics: list[MobileCriticalMetric],
) -> str:
    """Build the initial Claude turn for critical mobile monetisation anomalies."""
    metric_blocks: list[str] = []
    for metric in metrics:
        card = (
            f"<{metric.query_url}|{metric.query_title}>" if metric.query_url else metric.query_title
        )
        lines = [f"- {metric.platform} | {card}", f"  {metric.metric_line}"]
        lines.extend(f"  {diagnostic}" for diagnostic in metric.diagnostics)
        metric_blocks.append("\n".join(lines))
    selected = "\n".join(metric_blocks)
    report_header = report_text.partition("─")[0].strip()
    return f"""\
Use the `time-series-research` skill to investigate the critical negative anomalies from the
UG Mobile Monetisation Health Monitoring report below. Work from
`/home/egor/projects/ug-ai-analyst`, follow its CLAUDE.md and freshness preflight, and use its
analytics context and data tools.

Before querying, read these repository knowledge sources:
- `context/data-warehouse/anomaly-detection.md` for the detector semantics, alert vocabulary,
  thresholds, and the raw-source investigation playbook.
- `context/data-warehouse/tables/ug_mobile_health_monitoring.md` for metric definitions,
  datamart lineage, intermediate feeders, and raw-event mappings.

Investigate only these selected red critical metrics (at most five across Android and iOS):
{selected}

Source report summary:
{report_header}

The detector already compares sudden and sustained movement against a trend-plus-weekday
baseline. Treat its Z, drift, Δexp, Δwk, numerator/denominator, and segment-explainer output as
starting evidence, not as the conclusion. Use the linked Metabase cards and the documented
datamart/raw-source workflow to determine whether the movement is a real product/business issue,
a segment or composition shift, an experiment/release effect, or a freshness/pipeline artifact.
Look for a shared cause when several metrics move together; do not force one if evidence differs.

Reply in this Slack thread with a concise, self-contained investigation: findings by affected
platform/metric, evidence, likely cause, confidence, impact, and concrete recommended next
action. Say plainly when the evidence is inconclusive or points to a transient deviation.
Do not merely paraphrase the alert or repeat the detector calculations.
"""


class MobileHealthResponder:
    """Collect a possibly split report and launch one durable Claude investigation."""

    def __init__(self, settings: Settings, agent: AgentSubmitter) -> None:
        self.settings = settings
        self.agent = agent
        self._in_flight: set[str] = set()

    async def handle(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        message_ts = event.get("ts")
        text = event.get("text")
        if not isinstance(message_ts, str) or not isinstance(text, str):
            return
        if message_ts in self._in_flight:
            return
        self._in_flight.add(message_ts)
        try:
            report_text = await self._collect_report(client, message_ts, text)
            metrics = parse_critical_mobile_metrics(report_text)
            if not metrics:
                LOGGER.info("Mobile health report %s has no red critical metrics", message_ts)
                return
            await self.agent.submit(
                client,
                channel_id=self.settings.mobile_health_alert_channel,
                message_ts=f"{message_ts}:mobile-health-analysis",
                thread_ts=message_ts,
                text=build_mobile_health_agent_prompt(report_text, metrics),
                show_status=False,
            )
        finally:
            self._in_flight.discard(message_ts)

    async def _collect_report(
        self,
        client: AsyncWebClient,
        message_ts: str,
        initial_text: str,
    ) -> str:
        """Include webhook continuation chunks posted immediately after the header."""
        await asyncio.sleep(1)
        response = await client.conversations_history(
            channel=self.settings.mobile_health_alert_channel,
            oldest=message_ts,
            inclusive=True,
            limit=20,
        )
        chunks: list[tuple[float, str]] = []
        messages: list[dict[str, Any]] = response.get("messages", [])
        for message in messages:
            timestamp = message.get("ts")
            text = message.get("text")
            if (
                message.get("bot_id") == self.settings.mobile_health_bot_id
                and isinstance(timestamp, str)
                and isinstance(text, str)
                and 0 <= float(timestamp) - float(message_ts) <= 10
            ):
                if timestamp != message_ts and (
                    REPORT_MARKER in text or LEGACY_REPORT_MARKER in text
                ):
                    continue
                chunks.append((float(timestamp), text))
        if not chunks:
            return initial_text
        chunks.sort()
        return "\n".join(text for _, text in chunks)
