"""Launch durable investigations for critical web health drops."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.analysis_reuse import recent_analysis_reuse_policy
from sloperator.anomaly_alerts import AgentSubmitter
from sloperator.automated_session_policy import (
    AUTOMATED_RESPONSE_STYLE,
    AUTOMATED_SESSION_REPOSITORY_POLICY,
)
from sloperator.config import Settings

LOGGER = logging.getLogger(__name__)
REPORT_MARKER = "UG Monetisation: WEB health monitoring"
QUERY_RE = re.compile(
    r"^(?:(?::rotating_light:|🚨)\s+)?"
    r"<(?P<url>https?://[^|>]+)\|(?P<title>.+)>$"
)
CRITICAL_DROP_RE = re.compile(
    r"^(?::red_circle:|🔴)\s+(?P<metric>.+?)\s+\((?P<details>.*\bcritical\b.*)\)$"
)


@dataclass(frozen=True, slots=True)
class WebCriticalMetric:
    query_title: str
    query_url: str
    metric_line: str
    diagnostics: tuple[str, ...] = ()


def is_web_health_trigger(event: dict[str, Any], settings: Settings) -> bool:
    """Match only the configured monitor's top-level web dashboard report."""
    text = event.get("text")
    return (
        event.get("channel") == settings.mobile_health_alert_channel
        and event.get("bot_id") == settings.mobile_health_bot_id
        and not isinstance(event.get("thread_ts"), str)
        and isinstance(text, str)
        and REPORT_MARKER in text
    )


def parse_critical_web_metrics(text: str, *, limit: int = 5) -> list[WebCriticalMetric]:
    """Extract at most ``limit`` red critical metrics from the web report section."""
    selected: list[WebCriticalMetric] = []
    in_web_section = False
    query_title = ""
    query_url = ""
    diagnostic_target: int | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "Other":
            in_web_section = True
            query_title = ""
            query_url = ""
            diagnostic_target = None
            continue
        if line.startswith((":robot_face:", ":green_apple:", "🤖", "🍏")):
            in_web_section = False
            continue
        if not in_web_section:
            continue
        query_match = QUERY_RE.match(line)
        if query_match is not None:
            query_title = query_match.group("title")
            query_url = query_match.group("url")
            diagnostic_target = None
            continue
        if line.startswith((":mag:", ":bar_chart:", "🔍", "📊")) and diagnostic_target is not None:
            previous = selected[diagnostic_target]
            selected[diagnostic_target] = WebCriticalMetric(
                previous.query_title,
                previous.query_url,
                previous.metric_line,
                (*previous.diagnostics, line),
            )
            continue
        if line.startswith((":red_circle:", ":large_green_circle:", "🔴", "🟢")):
            diagnostic_target = None
        if len(selected) >= limit or CRITICAL_DROP_RE.match(line) is None:
            continue
        selected.append(
            WebCriticalMetric(query_title or "Unknown dashboard card", query_url, line)
        )
        diagnostic_target = len(selected) - 1
    return selected


def build_web_health_agent_prompt(
    report_text: str,
    metrics: list[WebCriticalMetric],
    channel_id: str = "C0AJKHFHVHV",
) -> str:
    """Build the initial agent turn for critical web monetisation anomalies."""
    metric_blocks: list[str] = []
    for metric in metrics:
        card = (
            f"<{metric.query_url}|{metric.query_title}>"
            if metric.query_url
            else metric.query_title
        )
        lines = [f"- Web | {card}", f"  {metric.metric_line}"]
        lines.extend(f"  {diagnostic}" for diagnostic in metric.diagnostics)
        metric_blocks.append("\n".join(lines))
    selected = "\n".join(metric_blocks)
    report_header = report_text.partition("─")[0].strip()
    return f"""\
Use the `time-series-research` skill to investigate the critical negative anomalies from the
UG Web Monetisation Health Monitoring report below. Work from
`/home/egor/projects/ug-ai-analyst`, follow its CLAUDE.md and freshness preflight, and use its
analytics context and data tools.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_RESPONSE_STYLE}

{recent_analysis_reuse_policy(
    channel_id,
    "platform + metric/card",
    "<@U0149RHN7D3> <@U09CYCGN6H4> <@U0525MDT0MN>",
)}

Before querying, read these repository knowledge sources:
- `context/data-warehouse/anomaly-detection.md` for the detector semantics, alert vocabulary,
  thresholds, and raw-source investigation playbook.
- `context/data-warehouse/dashboards/ug_web_health_monitoring.md` for dashboard 104's card
  families, raw SQL sources, metric definitions, timezone rules, bot filters, intro-plan merge
  logic, and known card-specific landmines.

Investigate only these selected red critical Web metrics (at most five):
{selected}

Source report summary:
{report_header}

The detector already compares sudden and sustained movement against a trend-plus-weekday
baseline. Treat its Z, drift, Δexp, Δwk, and any explainer output as starting evidence, not
as the conclusion. Dashboard 104 has no shared datamart: open each linked Metabase card and use
that card's exact SQL, filters, timezone, identity, and bot definition. Determine whether the
movement is a real product/business issue, a funnel or traffic-composition shift, an
experiment/release effect, fraud/bot activity, or a freshness/query artifact. Look for a shared
cause when several metrics move together; do not force one if evidence differs.

When no reusable analysis exists, do the full investigation and separate the detailed deliverable
from the Slack response:
- Create a detailed self-contained HTML report with the evidence walkthrough, charts, diagnostic
  cuts, calculations, rejected hypotheses, limitations, and source links needed to audit the
  conclusion. Preserve useful detail; do not shorten the report to match the Slack TL;DR.
- Package that report and the useful supporting SQL, scripts, and reader-safe data extracts into
  the required ZIP archive. Return the normal `SLOPERATOR_ARTIFACT` marker so Sloperator attaches
  the archive to this thread. The archive remains mandatory even when the conclusion is simple.
- Put evidence walkthroughs, "what it is not" inventories, and multi-step action lists only in the
  attached report, never in the visible Slack text.

For a fresh investigation, reply in this Slack thread with a TL;DR only, not a report. For each
affected metric, use one bold
Slack header line in the form `**Web | metric name**`, followed by exactly these five one-line fields
in this order, with no sub-bullets and no extra prose under or between them:
**Alert:** <real, noise, mean-reversion, or transient — one plain sentence>
**Cause:** <underlying issue, or "none found — alert fully explained by the above">
**Confidence:** <high/medium/low> (<one short reason>)
**Impact:** <one concrete number> (<optional one-clause context>)
**Next:** <one sentence containing 1-2 concrete next steps>

The bold field labels above are Markdown formatting, not code. Never wrap a complete field line in
backticks or a code fence.

The first Slack-facing response containing the analysis must begin with exactly this mention line:
`<@U0149RHN7D3> <@U09CYCGN6H4> <@U0525MDT0MN>`
Keep the mention line separate from the metric blocks; it is the only additional visible line
allowed. Budget exactly six visible lines per metric: one header plus the five fields. Do not
merely paraphrase the alert or repeat detector calculations. If evidence is inconclusive, say so
in `Alert` or `Cause`, keep confidence/impact/next minimal and factual, and do not speculate.
"""


class WebHealthResponder:
    """Collect a possibly split report and launch one durable agent investigation."""

    def __init__(self, settings: Settings, agent: AgentSubmitter) -> None:
        self.settings = settings
        self.agent = agent
        self._in_flight: set[str] = set()

    async def handle(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        message_ts = event.get("ts")
        text = event.get("text")
        if (
            not isinstance(message_ts, str)
            or not isinstance(text, str)
            or message_ts in self._in_flight
        ):
            return
        self._in_flight.add(message_ts)
        try:
            report_text = await self._collect_report(client, message_ts, text)
            metrics = parse_critical_web_metrics(report_text)
            if not metrics:
                LOGGER.info("Web health report %s has no red critical metrics", message_ts)
                return
            await self.agent.submit(
                client,
                channel_id=self.settings.mobile_health_alert_channel,
                message_ts=f"{message_ts}:web-health-analysis",
                thread_ts=message_ts,
                text=build_web_health_agent_prompt(
                    report_text,
                    metrics,
                    self.settings.mobile_health_alert_channel,
                ),
                show_status=False,
                require_artifact=True,
                timeout_seconds=self.settings.mobile_health_timeout_seconds,
                automated=True,
                reuse_key="web:" + repr(sorted(
                    (metric.query_title, metric.metric_line.split(" | ", 1)[0])
                    for metric in metrics
                )),
                reuse_mention_line="<@U0149RHN7D3> <@U09CYCGN6H4> <@U0525MDT0MN>",
            )
        finally:
            self._in_flight.discard(message_ts)

    async def _collect_report(
        self,
        client: AsyncWebClient,
        message_ts: str,
        initial_text: str,
    ) -> str:
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
                if timestamp != message_ts and REPORT_MARKER in text:
                    continue
                chunks.append((float(timestamp), text))
        if not chunks:
            return initial_text
        chunks.sort()
        return "\n".join(text for _, text in chunks)
