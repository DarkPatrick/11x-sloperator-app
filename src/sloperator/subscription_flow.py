"""Launch one durable investigation per distinct subscription-flow incident."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.anomaly_alerts import AgentSubmitter
from sloperator.config import Settings
from sloperator.store import EventStore

LOGGER = logging.getLogger(__name__)

SERIOUS_MARKER = ":rotating_light: *SERIOUS —"
RECOVERED_MARKER = ":white_check_mark: *Recovered —"
TITLE_RE = re.compile(r"SERIOUS — (?P<platforms>.+?) (?P<kind>.+?) anomaly\*")
SECTION_RE = re.compile(r"^\*(?P<label>[^*]+)\* — ")
RECOVERY_RE = re.compile(r"Recovered — (?P<label>[^*]+)\*")


@dataclass(frozen=True, slots=True)
class SubscriptionFlowIncident:
    nature_key: str
    components: frozenset[str]
    alert_text: str


def is_subscription_flow_event(event: dict[str, Any], settings: Settings) -> bool:
    """Match only serious alerts or recovery replies in the configured channel."""
    text = event.get("text")
    return (
        event.get("channel") == settings.subscription_flow_alert_channel
        and event.get("bot_id") is not None
        and isinstance(text, str)
        and (SERIOUS_MARKER in text or RECOVERED_MARKER in text)
    )


def _flow_kind(value: str) -> str:
    lowered = value.lower()
    if "new subscriptions" in lowered or "first purchases" in lowered:
        return "acquisitions"
    if "recurring charges" in lowered:
        return "recurring"
    if "renewals" in lowered:
        return "renewals"
    return re.sub(r"[^a-z0-9]+", "_", lowered).strip("_") or "unknown"


def _platform(value: str) -> str:
    lowered = value.lower()
    for platform in ("android", "ios", "web"):
        if platform in lowered:
            return platform
    return re.sub(r"[^a-z0-9]+", "_", lowered).strip("_") or "unknown"


def _state(line: str) -> str:
    if ":red_circle:" in line:
        return "serious"
    if ":large_yellow_circle:" in line:
        return "watch"
    if ":large_green_circle:" in line:
        return "ok"
    if "_data unavailable_" in line:
        return "unknown"
    if "_insufficient baseline_" in line:
        return "insufficient"
    return "unspecified"


def parse_serious_incident(text: str) -> SubscriptionFlowIncident | None:
    """Derive a cross-platform incident nature from leg/probe states, not changing values."""
    title = TITLE_RE.search(text)
    if title is None:
        return None
    title_kind = _flow_kind(title.group("kind"))
    title_platforms = {
        platform
        for platform in ("android", "ios", "web")
        if platform in title.group("platforms").lower()
    }
    components: set[str] = set()
    signatures: list[tuple[str, str, str]] = []
    current_platform: str | None = None
    upstream = downstream = probe = "unspecified"

    def finish_section() -> None:
        nonlocal upstream, downstream, probe
        if current_platform is not None:
            components.add(f"{current_platform}:{title_kind}")
            signatures.append((upstream, downstream, probe))
        upstream = downstream = probe = "unspecified"

    for line in text.splitlines():
        section = SECTION_RE.match(line)
        if section:
            finish_section()
            current_platform = _platform(section.group("label"))
            continue
        stripped = line.strip()
        if stripped.startswith("• Upstream"):
            upstream = _state(stripped)
        elif stripped.startswith("• Downstream"):
            downstream = _state(stripped)
        elif stripped.startswith("• Ingestion check:"):
            if "stalled" in stripped.lower():
                probe = "stalled"
            elif "healthy" in stripped.lower():
                probe = "healthy"
            else:
                probe = "unknown"
    finish_section()
    if not components:
        components = {f"{platform}:{title_kind}" for platform in title_platforms}
    if not components:
        return None
    stable_signatures = sorted(set(signatures)) or [("unspecified",) * 3]
    canonical = "|".join(":".join(signature) for signature in stable_signatures)
    nature_key = hashlib.sha256(canonical.encode()).hexdigest()
    return SubscriptionFlowIncident(nature_key, frozenset(components), text)


def parse_recovered_component(text: str) -> str | None:
    """Map the monitor's recovery title to the same platform-and-kind component key."""
    match = RECOVERY_RE.search(text)
    if match is None:
        return None
    label = match.group("label")
    platform = _platform(label)
    kind = _flow_kind(label)
    if "unknown" in {platform, kind}:
        return None
    return f"{platform}:{kind}"


def build_subscription_flow_agent_prompt(incident: SubscriptionFlowIncident) -> str:
    """Give the analyst the detector semantics plus the exact alert that fired."""
    components = ", ".join(sorted(incident.components))
    return f"""\
Use the `time-series-research` skill to investigate this SERIOUS UG subscription-flow alert.
Work from `/home/egor/projects/ug-ai-analyst`, follow AGENTS.md and its freshness preflight,
and use the repository's analytics context and data tools.

Detector context:
- Each affected flow compares an upstream store/processor signal with the corresponding
  downstream `ug_subscriptions_events` signal against trailing time-aware baselines.
- SERIOUS means a catastrophic single-leg collapse or a corroborated/sustained drop, depending
  on the platform and flow shown in the alert.
- The ingestion check is a near-real-time Graylog liveness probe and may distinguish our receipt
  pipeline from a store-side or proxy/replica issue.
- A green opposite leg is important evidence: do not treat every alert as a real sales drop.

Affected components: {components}

Exact Slack alert:
--- begin alert ---
{incident.alert_text}
--- end alert ---

Investigate the most likely shared cause, including source freshness, proxy/replica health,
ingestion lag, relevant recent changes, and whether business volumes actually moved. Reply in
this Slack thread with a concise evidence-backed diagnosis, confidence, impact, and recommended
next action. If it is likely a delayed replica/proxy or a transient one-off, say so plainly.
Do not merely paraphrase the detector's built-in diagnosis.
"""


class SubscriptionFlowResponder:
    """Persist incident state and launch Claude only for a new incident nature."""

    def __init__(
        self,
        settings: Settings,
        store: EventStore,
        agent: AgentSubmitter,
    ) -> None:
        self.settings = settings
        self.store = store
        self.agent = agent
        self._own_user_id: str | None = None

    async def handle(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        if not await self._is_own_bot(event, client):
            LOGGER.warning("Ignoring subscription-flow-shaped message from another bot")
            return
        text = event.get("text")
        message_ts = event.get("ts")
        if not isinstance(text, str) or not isinstance(message_ts, str):
            return
        if RECOVERED_MARKER in text:
            component = parse_recovered_component(text)
            recovered_alert_ts = event.get("thread_ts")
            if component is not None and isinstance(recovered_alert_ts, str):
                resolved = await asyncio.to_thread(
                    self.store.recover_subscription_flow_component,
                    component,
                    recovered_alert_ts,
                )
                LOGGER.info(
                    "Subscription-flow component %s recovered; resolved incidents=%d",
                    component,
                    resolved,
                )
            return
        incident = parse_serious_incident(text)
        if incident is None:
            LOGGER.warning("Could not parse SERIOUS subscription-flow alert %s", message_ts)
            return
        should_launch = await asyncio.to_thread(
            self.store.claim_subscription_flow_incident,
            incident.nature_key,
            set(incident.components),
            message_ts,
        )
        if not should_launch:
            LOGGER.info(
                "Suppressing repeated subscription-flow incident nature %s",
                incident.nature_key[:12],
            )
            return
        channel = self.settings.subscription_flow_alert_channel
        await self.agent.submit(
            client,
            channel_id=channel,
            message_ts=f"{message_ts}:subscription-flow-analysis",
            thread_ts=message_ts,
            text=build_subscription_flow_agent_prompt(incident),
            show_status=False,
        )

    async def _is_own_bot(
        self,
        event: dict[str, Any],
        client: AsyncWebClient,
    ) -> bool:
        if self._own_user_id is None:
            response = await client.auth_test()
            user_id = response.get("user_id")
            if not isinstance(user_id, str):
                return False
            self._own_user_id = user_id
        return event.get("user") == self._own_user_id
