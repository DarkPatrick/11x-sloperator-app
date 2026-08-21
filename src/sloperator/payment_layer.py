"""Launch durable investigations for serious payment-layer alerts."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.anomaly_alerts import AgentSubmitter
from sloperator.automated_session_policy import (
    AUTOMATED_RESPONSE_STYLE,
    AUTOMATED_SESSION_REPOSITORY_POLICY,
)
from sloperator.config import Settings
from sloperator.store import EventStore

LOGGER = logging.getLogger(__name__)

ERROR_MARKER = "Payment path error"
COLLAPSE_MARKER = "Payment path collapsed"


def is_payment_layer_trigger(event: dict[str, Any], settings: Settings) -> bool:
    """Match top-level serious payment alerts in the configured channel."""
    text = event.get("text")
    return (
        event.get("channel") == settings.payment_layer_alert_channel
        and event.get("bot_id") is not None
        and not isinstance(event.get("thread_ts"), str)
        and isinstance(text, str)
        and (ERROR_MARKER in text or COLLAPSE_MARKER in text)
    )


def build_payment_layer_agent_prompt(alert_text: str) -> str:
    """Build the first Claude turn for a serious payment-path incident."""
    return f"""\
Use the `time-series-research` skill to investigate this serious UG payment-layer alert.
Work from `/home/egor/projects/ug-ai-analyst`, follow CLAUDE.md and its freshness preflight,
and use the repository's analytics context and data tools.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_RESPONSE_STYLE}

The detector compares one payment-path cell `(app source, app build, path type)` with the same
path before the build rolled out. A `Payment path error` alert means a new store-error signature
has become common in that cell. A `Payment path collapsed` alert means start-to-finish conversion
fell catastrophically with a strict significance and estimated-loss gate.

Before drawing conclusions, read:
- `scripts/payment_layer_monitoring_cron.md` for detector semantics, thresholds, backtests, known
  first-day false positives, and the intended incident workflow;
- `scripts/payment_error_signature_monitor.py`, `scripts/payment_completion_class_a_monitor.py`,
  and `scripts/payment_monitor_common.py` for the exact query, path classification, baseline,
  allowlist, loss estimate, and incident/recovery state logic;
- the repository monetisation metric sources required by CLAUDE.md before interpreting payment,
  subscription, conversion, or revenue numbers.

Exact Slack alert:
--- begin alert ---
{alert_text}
--- end alert ---

Investigate in this order:
1. Reproduce the alerted cell from ClickHouse and verify source freshness, rollout date, sample
   size, baseline window, path classification, and whether Layer 0 and Class A corroborate it.
2. Check whether the build is in its first rollout day, when finish events can be temporarily
   under-reported; an independent error signature on the same cell is stronger evidence.
3. Compare the affected build with the preceding production build and inspect the payment-related
   mobile code diff through the repository's supported read-only GitHub tooling. Focus on the
   alerted path (`promo_offer`, `instant`, `intro`, or `plain`). If source access is unavailable,
   say so and continue with the data evidence instead of guessing.
4. Quantify the current net-revenue impact using the repository definitions, then identify the
   likely owner and the smallest concrete mitigation or verification step needed now.

Reply in this Slack thread with no more than five short lines: verdict, cause, impact, confidence,
and next action. Put detailed evidence in a compact attached artifact only when it materially helps
the incident response. Do not merely repeat the detector output.
"""


class PaymentLayerResponder:
    """Launch one durable Claude session in each new payment-alert thread."""

    def __init__(self, settings: Settings, store: EventStore, agent: AgentSubmitter) -> None:
        self.settings = settings
        self.store = store
        self.agent = agent
        self._own_user_id: str | None = None
        self._in_flight: set[str] = set()

    async def handle(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        if not await self._is_own_bot(event, client):
            LOGGER.warning("Ignoring payment-layer-shaped message from another bot")
            return
        message_ts = event.get("ts")
        text = event.get("text")
        if not isinstance(message_ts, str) or not isinstance(text, str):
            return
        if message_ts in self._in_flight:
            return
        self._in_flight.add(message_ts)
        try:
            if await asyncio.to_thread(
                self.store.has_agent_thread,
                self.settings.payment_layer_alert_channel,
                message_ts,
            ):
                return
            await self.agent.submit(
                client,
                channel_id=self.settings.payment_layer_alert_channel,
                message_ts=f"{message_ts}:payment-layer-analysis",
                thread_ts=message_ts,
                text=build_payment_layer_agent_prompt(text),
                show_status=False,
                automated=True,
            )
        finally:
            self._in_flight.discard(message_ts)

    async def _is_own_bot(self, event: dict[str, Any], client: AsyncWebClient) -> bool:
        if self._own_user_id is None:
            response = await client.auth_test()
            user_id = response.get("user_id")
            if not isinstance(user_id, str):
                return False
            self._own_user_id = user_id
        return event.get("user") == self._own_user_id
