from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.config import Settings
from sloperator.payment_layer import (
    PaymentLayerResponder,
    build_payment_layer_agent_prompt,
    is_payment_layer_trigger,
)
from sloperator.store import EventStore

ALERT = (
    ":rotating_light: *Payment path collapsed* — UGT_IOS 7.3.23 `promo_offer`\n"
    "0.0% vs 66.3% · ~-$394/day\n"
    "<@U1> <@U2> · investigation in thread"
)


def _settings() -> Settings:
    return Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")


def test_trigger_requires_channel_top_level_bot_message_and_marker() -> None:
    settings = _settings()
    event = {
        "channel": settings.payment_layer_alert_channel,
        "bot_id": "BSELF",
        "ts": "100.1",
        "text": ALERT,
    }
    assert is_payment_layer_trigger(event, settings)
    assert not is_payment_layer_trigger({**event, "channel": "COTHER"}, settings)
    assert not is_payment_layer_trigger({**event, "bot_id": None}, settings)
    assert not is_payment_layer_trigger({**event, "thread_ts": "99.1"}, settings)


def test_prompt_is_concise_and_keeps_automated_session_policy() -> None:
    prompt = build_payment_layer_agent_prompt(ALERT)
    assert "`time-series-research`" in prompt
    assert "CLAUDE.md" in prompt
    assert "AUTOMATED SESSION REPOSITORY BOUNDARY" in prompt
    assert "scripts/payment_layer_monitoring_cron.md" in prompt
    assert "mobile code diff" in prompt
    assert "Layer 0 and Class A corroborate" in prompt
    assert "no more than five short lines" in prompt
    assert ALERT in prompt


@pytest.mark.asyncio
async def test_concurrent_delivery_launches_one_agent(tmp_path) -> None:
    settings = _settings()
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    agent = SimpleNamespace(submit=AsyncMock())
    client = SimpleNamespace(auth_test=AsyncMock(return_value={"user_id": "USELF"}))
    responder = PaymentLayerResponder(settings, store, agent)
    event = {
        "channel": settings.payment_layer_alert_channel,
        "user": "USELF",
        "bot_id": "BSELF",
        "ts": "100.1",
        "text": ALERT,
    }

    await asyncio.gather(
        responder.handle(dict(event), client),
        responder.handle(dict(event), client),
    )

    agent.submit.assert_awaited_once()
    assert agent.submit.await_args.kwargs["thread_ts"] == "100.1"
    assert agent.submit.await_args.kwargs["automated"] is True
