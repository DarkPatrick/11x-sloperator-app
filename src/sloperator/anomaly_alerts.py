"""Event-driven validation and replies for Analytics Bot anomaly alerts."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import aiohttp
from slack_sdk.web.async_client import AsyncWebClient

from sloperator.automated_session_policy import (
    AUTOMATED_RESPONSE_STYLE,
    AUTOMATED_SESSION_REPOSITORY_POLICY,
)
from sloperator.config import Settings
from sloperator.store import EventStore

LOGGER = logging.getLogger(__name__)

HEADER_DT_RE = re.compile(
    r"(?:for|за)\s*\*\s*(?P<dt>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\(UTC\)\*"
)
ALERT_RE = re.compile(
    r"\*(?P<metric>.+?)\*,\s*"
    r"\*(?P<platform>[^*]+?)\*,\s*"
    r"(?:change in|изменение)\s*\*(?P<type>events|uniques|событий|уников)\*\s*"
    r"(?P<diff>[+\-\N{MINUS SIGN}][0-9.]+|∞)%\s*\|\s*"
    r"(?:was|было):\s*(?P<was>\d+),\s*"
    r"(?:expected|ожидалось):\s*(?P<expected>\d+)\s*\|\s*"
    r"p-value:\s*(?P<pval>[0-9.]+)"
)
TYPE_MAP = {
    "событий": "events",
    "уников": "uniques",
    "events": "events",
    "uniques": "uniques",
}
ALERT_DT_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
ALERT_WINDOW_HOURS = 3
REDASH_QUERY_URL = "https://redash.ultimate-guitar.com/queries/27828/source"
REDASH_FRAGMENT = "#56607"
TABLE_COLUMNS = [
    "metric",
    "platform",
    "type",
    "Δ vs prophet",
    "value",
    "last week",
    "WoW",
    "peak WoW",
    "verdict",
    "redash",
]
# Pinned from analytics-airflow-dags@feff00a5:
# monitorings/ug_anomalies/cfg_consts.py -> mention_groups["ug_monetisation"]["metrics"].
MONETISATION_METRICS = frozenset(
    {
        "Landing Upgrade Open",
        "Tour Start",
        "Landing Checkout View",
        "Landing Sign Up Success",
        "Landing Plans View",
        "Landing Purchase",
        "Banner View:from Tour",
        "Banner View:from App",
        "Banner Purchase Click:from Tour",
        "Banner Purchase Click:from App",
        "Purchase:from Tour",
        "Purchase:from App",
        "All Subscription Events",
        "Splash View",
    }
)
WOW_QUERY = """
select
    `mel`.`datetime` as `datetime`,
    `mel`.`metric` as `metric`,
    `mel`.`platform` as `platform`,
    `mel`.`metric_type` as `metric_type`,
    argMax(`mel`.`value`, `mel`.`add_datetime`) as `value`,
    argMax(`mel`.`prediction`, `mel`.`add_datetime`) as `prediction`
from `local`.`monitoring_errors_local` as `mel`
where
    `mel`.`date` >= toDate(parseDateTimeBestEffort('{alert_dt}')) - interval 9 day
    and `mel`.`date` <= toDate(parseDateTimeBestEffort('{alert_dt}')) + interval 1 day
    and `mel`.`datetime` <= parseDateTimeBestEffort('{alert_dt}')
    and `mel`.`datetime` > parseDateTimeBestEffort('{alert_dt}')
        - interval {floor_hours} hour
    and `mel`.`product` = 'UG'
    and (`mel`.`metric`, `mel`.`platform`, `mel`.`metric_type`) in ({tuples})
group by `datetime`, `metric`, `platform`, `metric_type`
format JSON
""".strip()


@dataclass(frozen=True, slots=True)
class Alert:
    metric: str
    platform: str
    metric_type: str
    diff: str
    was: int
    expected: int
    pval: str

    def key(self) -> tuple[str, str, str]:
        return self.metric, self.platform, self.metric_type


@dataclass(slots=True)
class AlertBatch:
    alert_dt: str | None
    header_ts: str
    mention_ts: str | None = None
    alerts: list[Alert] = field(default_factory=list)


class AgentSubmitter(Protocol):
    """Narrow agent-orchestrator interface used by the alert responder."""

    def submit(
        self,
        client: AsyncWebClient,
        *,
        channel_id: str,
        message_ts: str,
        thread_ts: str,
        text: str,
        show_status: bool = True,
        require_artifact: bool = False,
        automated: bool = False,
        timeout_seconds: int | None = None,
    ) -> Awaitable[object]: ...


def is_anomaly_trigger(event: dict[str, Any], settings: Settings) -> bool:
    """Match only Analytics Bot mentioning the configured operator in the target channel."""
    text = event.get("text")
    return (
        event.get("channel") == settings.anomaly_alert_channel
        and isinstance(text, str)
        and f"<@{settings.slack_user_id}>" in text
        and (
            event.get("user") == settings.anomaly_bot_user_id
            or (
                bool(settings.anomaly_bot_id)
                and event.get("bot_id") == settings.anomaly_bot_id
            )
        )
    )


def build_batches(
    messages: list[tuple[str, str]],
    operator_user_id: str,
) -> list[AlertBatch]:
    """Reassemble split Analytics Bot messages into chronological alert batches."""
    batches: list[AlertBatch] = []
    current: AlertBatch | None = None
    seen_keys: set[tuple[str, str, str]] = set()
    for timestamp, body in messages:
        for line in body.splitlines():
            header = HEADER_DT_RE.search(line)
            if header:
                current = AlertBatch(header.group("dt"), timestamp)
                seen_keys = set()
                batches.append(current)
            if f"<@{operator_user_id}>" in line:
                if current is None:
                    current = AlertBatch(None, timestamp)
                    seen_keys = set()
                    batches.append(current)
                current.mention_ts = timestamp
            match = ALERT_RE.search(line)
            if not match:
                continue
            if current is None:
                current = AlertBatch(None, timestamp)
                seen_keys = set()
                batches.append(current)
            alert = Alert(
                metric=match.group("metric").strip(),
                platform=match.group("platform").strip(),
                metric_type=TYPE_MAP[match.group("type")],
                diff=match.group("diff").replace("\N{MINUS SIGN}", "-"),
                was=int(match.group("was")),
                expected=int(match.group("expected")),
                pval=match.group("pval"),
            )
            if alert.key() not in seen_keys:
                seen_keys.add(alert.key())
                current.alerts.append(alert)
    return batches


class AnomalyAlertResponder:
    """Fetch a just-completed alert batch, validate it, and reply to its mention message."""

    def __init__(
        self,
        settings: Settings,
        store: EventStore,
        agent: AgentSubmitter,
    ) -> None:
        self.settings = settings
        self.store = store
        self.agent = agent
        self._in_flight: set[str] = set()

    async def handle(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        trigger_ts = event.get("ts")
        if not isinstance(trigger_ts, str):
            return
        if trigger_ts in self._in_flight:
            LOGGER.info("Anomaly alert %s is already being processed", trigger_ts)
            return
        self._in_flight.add(trigger_ts)
        try:
            batch = await self._find_batch(client, trigger_ts)
            if batch is None or not batch.alerts:
                LOGGER.warning("No parseable anomaly batch found for Slack message %s", trigger_ts)
                return
            if await self._already_replied(client, trigger_ts):
                LOGGER.info("Anomaly alert %s already has a Sloperator reply", trigger_ts)
                return
            try:
                rows = await self._query_clickhouse(build_batch_sql(batch, self.settings))
            except Exception:
                LOGGER.exception(
                    "ClickHouse validation failed for anomaly alert %s; leaving it unanswered",
                    trigger_ts,
                )
                return
            results = [
                verdict_for_metric(rows, alert, batch.alert_dt, self.settings)
                for alert in batch.alerts
            ]
            text, blocks = render_blocks(batch, results, self.settings.anomaly_threshold)
            await client.chat_postMessage(
                channel=self.settings.anomaly_alert_channel,
                thread_ts=trigger_ts,
                text=text,
                blocks=blocks,
                unfurl_links=False,
            )
            monetisation = confirmed_monetisation_anomalies(batch, results)
            if monetisation:
                claimed = await asyncio.to_thread(
                    self.store.claim_anomaly_analyses,
                    [alert.key() for alert, _ in monetisation],
                )
                monetisation = [
                    item for item in monetisation if item[0].key() in claimed
                ]
            if monetisation:
                await self.agent.submit(
                    client,
                    channel_id=self.settings.anomaly_alert_channel,
                    message_ts=f"{trigger_ts}:monetisation-analysis",
                    thread_ts=trigger_ts,
                    text=build_monetisation_agent_prompt(batch, monetisation),
                    show_status=False,
                    require_artifact=True,
                    automated=True,
                )
        finally:
            self._in_flight.discard(trigger_ts)

    async def _find_batch(
        self,
        client: AsyncWebClient,
        trigger_ts: str,
    ) -> AlertBatch | None:
        oldest = time.time() - self.settings.anomaly_window_hours * 3600
        messages: list[tuple[str, str]] = []
        cursor: str | None = None
        while True:
            response = await client.conversations_history(
                channel=self.settings.anomaly_alert_channel,
                oldest=f"{oldest:.6f}",
                limit=200,
                cursor=cursor,
            )
            raw_messages: list[dict[str, Any]] = response.get("messages", [])
            for message in raw_messages:
                if (
                    message.get("user") == self.settings.anomaly_bot_user_id
                    or (
                        bool(self.settings.anomaly_bot_id)
                        and message.get("bot_id") == self.settings.anomaly_bot_id
                    )
                ):
                    timestamp = message.get("ts")
                    text = message.get("text")
                    if isinstance(timestamp, str) and isinstance(text, str):
                        messages.append((timestamp, text))
            next_cursor = (response.get("response_metadata") or {}).get("next_cursor")
            cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
            if cursor is None:
                break
        messages.sort(key=lambda item: float(item[0]))
        return next(
            (
                batch
                for batch in build_batches(messages, self.settings.slack_user_id)
                if batch.mention_ts == trigger_ts
            ),
            None,
        )

    async def _already_replied(
        self,
        client: AsyncWebClient,
        thread_ts: str,
    ) -> bool:
        auth = await client.auth_test()
        own_user_id = auth.get("user_id")
        if not isinstance(own_user_id, str):
            LOGGER.warning("Slack auth.test returned no bot user ID; skipping alert reply")
            return True
        response = await client.conversations_replies(
            channel=self.settings.anomaly_alert_channel,
            ts=thread_ts,
            limit=200,
        )
        reply_messages: list[dict[str, Any]] = response.get("messages", [])
        return any(
            message.get("ts") != thread_ts and message.get("user") == own_user_id
            for message in reply_messages
        )

    async def _query_clickhouse(self, sql: str) -> list[dict[str, Any]]:
        settings = self.settings
        if settings.clickhouse_host is None or settings.clickhouse_username is None:
            raise RuntimeError("CLICKHOUSE_HOST and CLICKHOUSE_USERNAME are not configured")
        url = f"https://{settings.clickhouse_host}:{settings.clickhouse_port}/"
        authorization = aiohttp.encode_basic_auth(
            settings.clickhouse_username,
            settings.clickhouse_password,
        )
        timeout = aiohttp.ClientTimeout(total=60)
        async with (
            aiohttp.ClientSession(
                headers={"Authorization": authorization},
                timeout=timeout,
            ) as session,
            session.post(
                url,
                data=sql.encode(),
                ssl=False,
            ) as response,
        ):
            body = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"ClickHouse returned HTTP {response.status}: {body[:300]}")
        payload = json.loads(body)
        data = payload.get("data")
        if not isinstance(data, list):
            raise RuntimeError("ClickHouse returned malformed JSON")
        return [row for row in data if isinstance(row, dict)]


def _sql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


def build_batch_sql(batch: AlertBatch, settings: Settings) -> str:
    if batch.alert_dt is None or not ALERT_DT_RE.fullmatch(batch.alert_dt):
        raise ValueError(f"unexpected alert_dt {batch.alert_dt!r}")
    seen: set[tuple[str, str, str]] = set()
    tuples: list[str] = []
    for alert in batch.alerts:
        if alert.key() in seen:
            continue
        seen.add(alert.key())
        values = ",".join(f"'{_sql_escape(value)}'" for value in alert.key())
        tuples.append(f"({values})")
    floor_hours = 24 * settings.anomaly_days_before + 168 + 1
    return (
        WOW_QUERY.replace("{alert_dt}", batch.alert_dt)
        .replace("{floor_hours}", str(floor_hours))
        .replace("{tuples}", ",".join(tuples))
    )


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def verdict_for_metric(
    rows: list[dict[str, Any]],
    alert: Alert,
    alert_dt: str | None,
    settings: Settings,
) -> dict[str, Any]:
    if alert_dt is None:
        return {"status": "no_anchor"}
    series: dict[str, tuple[float | None, float | None]] = {}
    for row in rows:
        key = str(row.get("metric")), str(row.get("platform")), str(row.get("metric_type"))
        if key == alert.key():
            series[str(row.get("datetime"))[:19]] = (
                _number(row.get("value")),
                _number(row.get("prediction")),
            )
    if not series:
        return {"status": "no_data"}
    base = dt.datetime.strptime(alert_dt, "%Y-%m-%d %H:%M:%S")

    def at(offset: int) -> tuple[float | None, float | None] | None:
        timestamp = (base - dt.timedelta(hours=offset)).strftime("%Y-%m-%d %H:%M:%S")
        return series.get(timestamp)

    def value(offset: int) -> float | None:
        cell = at(offset)
        return cell[0] if cell else None

    def wow(offset: int) -> float | None:
        current, last_week = value(offset), value(offset + 168)
        if current is None or last_week is None or last_week == 0:
            return None
        return current / last_week - 1

    alerted = at(0)
    current_value = alerted[0] if alerted else None
    prediction = alerted[1] if alerted else None
    last_week = value(168)
    wow_window = [wow(offset) for offset in range(ALERT_WINDOW_HOURS)]
    present = [item for item in wow_window if item is not None]
    peak = max(present, key=abs) if present else None
    result: dict[str, Any] = {
        "status": "ok",
        "value": current_value,
        "prediction": prediction,
        "last_week": last_week,
        "wow": wow_window[0],
        "peak_wow": peak,
    }
    if current_value is None:
        result["status"] = "no_data"
    elif not present:
        result["verdict"] = "no_last_week"
    else:
        assert peak is not None
        result["verdict"] = (
            "ANOMALY" if abs(peak) > settings.anomaly_threshold else "OK"
        )
    return result


def _cell(text: str) -> dict[str, Any]:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [{"type": "text", "text": str(text)}],
            }
        ],
    }


def _link_cell(url: str) -> dict[str, Any]:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [{"type": "link", "url": url, "text": "open ↗"}],
            }
        ],
    }


def _integer(value: Any) -> str:
    number = _number(value)
    return "?" if number is None else f"{round(number):,}"


def _percent(value: Any) -> str:
    number = _number(value)
    return "n/a" if number is None else f"{number * 100:+.1f}%"


def _redash_url(alert: Alert) -> str:
    return (
        f"{REDASH_QUERY_URL}?p_metric={quote(alert.metric, safe='')}"
        f"&p_metric_type={quote(alert.metric_type, safe='')}"
        f"&p_platform={quote(alert.platform, safe='')}{REDASH_FRAGMENT}"
    )


def render_blocks(
    batch: AlertBatch,
    results: list[dict[str, Any]],
    threshold: float,
) -> tuple[str, list[dict[str, Any]]]:
    anomalies: list[tuple[Alert, dict[str, Any], str]] = []
    false_alarms: list[Alert] = []
    undetermined: list[tuple[Alert, dict[str, Any], str]] = []
    for alert, result in zip(batch.alerts, results, strict=True):
        if result.get("status") != "ok":
            reason = {
                "no_data": "not in monitoring table",
                "no_anchor": "no datetime header in window",
            }.get(str(result.get("status")), "undetermined")
            undetermined.append((alert, result, f"⚠️ {reason}"))
        elif result.get("verdict") == "ANOMALY":
            direction = "down" if (_number(result.get("peak_wow")) or 0) < 0 else "up"
            anomalies.append((alert, result, f"🔴 real ({direction} WoW)"))
        elif result.get("verdict") == "OK":
            false_alarms.append(alert)
        else:
            undetermined.append((alert, result, "⚠️ no last-week data"))

    intro = (
        f"Week-over-week sanity check for this alert ({batch.alert_dt or 'UNKNOWN'} UTC) — "
        f"threshold ±{threshold * 100:.0f}%. {len(anomalies)} real anomaly(ies), "
        f"{len(false_alarms)} false alarm(s)"
        + (f", {len(undetermined)} undetermined" if undetermined else "")
        + f" of {len(batch.alerts)} alerted metric(s)."
    )
    blocks: list[dict[str, Any]] = [_cell(intro)]
    table_rows = anomalies + undetermined
    if table_rows:
        rows = [[_cell(column) for column in TABLE_COLUMNS]]
        for alert, result, verdict in table_rows:
            rows.append(
                [
                    _cell(alert.metric),
                    _cell(alert.platform),
                    _cell(alert.metric_type),
                    _cell(f"{alert.diff}%"),
                    _cell(_integer(result.get("value"))),
                    _cell(_integer(result.get("last_week"))),
                    _cell(_percent(result.get("wow"))),
                    _cell(_percent(result.get("peak_wow"))),
                    _cell(verdict),
                    _link_cell(_redash_url(alert)),
                ]
            )
        blocks.append({"type": "table", "rows": rows})
        if false_alarms:
            blocks.append(
                _cell(
                    f"Other {len(false_alarms)} metric(s) omitted — "
                    f"all within ±{threshold * 100:.0f}% WoW."
                )
            )
    else:
        blocks.append(
            _cell(
                "All alerted metrics are within the week-over-week tolerance — "
                "no action needed."
            )
        )
    return intro, blocks


def confirmed_monetisation_anomalies(
    batch: AlertBatch,
    results: list[dict[str, Any]],
) -> list[tuple[Alert, dict[str, Any]]]:
    """Keep confirmed anomalies belonging to the pinned UG monetisation group."""
    return [
        (alert, result)
        for alert, result in zip(batch.alerts, results, strict=True)
        if alert.metric in MONETISATION_METRICS
        and result.get("status") == "ok"
        and result.get("verdict") == "ANOMALY"
    ]


def build_monetisation_agent_prompt(
    batch: AlertBatch,
    anomalies: list[tuple[Alert, dict[str, Any]]],
) -> str:
    """Build the first durable agent turn for a confirmed monetisation incident."""
    metric_lines = []
    for alert, result in anomalies:
        metric_lines.append(
            "- "
            f"{alert.metric} | platform={alert.platform} | type={alert.metric_type} | "
            f"prophet_delta={alert.diff}% | value={_integer(result.get('value'))} | "
            f"last_week={_integer(result.get('last_week'))} | "
            f"wow={_percent(result.get('wow'))} | "
            f"peak_wow={_percent(result.get('peak_wow'))}"
        )
    metrics = "\n".join(metric_lines)
    return f"""\
Use the `time-series-research` skill to investigate the confirmed UG monetisation anomalies
below. Work from the current `/home/egor/projects/ug-ai-analyst` repository, follow its
CLAUDE.md and freshness preflight, and use its analytics context and data tools.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_RESPONSE_STYLE}

Alert timestamp: {batch.alert_dt or "unknown"} UTC
Confirmed monetisation anomalies:
{metrics}

Determine the most likely cause using relevant time-series cuts and supporting evidence.
Distinguish a persistent/product or technical issue from a one-off fluctuation. Reply in this
Slack thread with a concise, self-contained investigation: findings, confidence, likely cause,
and concrete recommended next action. If evidence points to a transient deviation, say so
plainly instead of forcing a root cause. Do not merely repeat the auto-reply calculation.

This is a routine automated Analytics Bot investigation. Prioritise a correct, timely Slack
answer while keeping the usual useful charts and visual-review workflow. Perfect visual polish
is not required: fix critical issues that could mislead the reader or obscure the evidence,
but if further review rounds are only producing non-critical cosmetic changes and consuming
substantial time, publish the current sound version instead of continuing to polish it.
Reserve enough time to package artifacts and return the final response before the deadline.

Before investigating, read `.claude/reusable_analyses/README.md` and any linked cases that
look similar. Reuse relevant metric definitions, diagnostic cuts, queries, report structure,
and prior findings where they still apply, while validating the current incident independently.
Do not add a case or update the index unless a human explicitly asks for that in this Slack thread.
"""
