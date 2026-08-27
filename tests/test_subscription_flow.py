from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.config import Settings
from sloperator.store import EventStore
from sloperator.subscription_flow import (
    SubscriptionFlowResponder,
    build_subscription_flow_agent_prompt,
    is_subscription_flow_event,
    parse_recovered_component,
    parse_serious_incident,
    parse_serious_incidents,
)


def _serious_alert(
    title: str,
    section: str,
    upstream_state: str = ":red_circle:",
    downstream_state: str = ":large_green_circle:",
    probe: str = "healthy",
) -> str:
    return f""":rotating_light: *SERIOUS — {title} anomaly* :rotating_light:
Evaluated hour (UTC): *2026-07-25 12:00*

*{section}* — diagnosis
    • Upstream — source: *0* vs baseline *100* → *0% of normal* {upstream_state}
    • Downstream — events: *100* vs baseline *100* → *100% of normal* {downstream_state}
    • Ingestion check: our ingestion is {probe} :large_green_circle:
"""


def test_same_leg_pattern_is_separate_across_platforms_and_kinds() -> None:
    recurring = parse_serious_incident(_serious_alert("Android iOS recurring charges", "Android"))
    acquisitions = parse_serious_incident(
        _serious_alert("Web new subscriptions / first purchases", "Web")
    )

    assert recurring is not None
    assert acquisitions is not None
    assert recurring.nature_key != acquisitions.nature_key
    assert recurring.components == {"android:recurring"}
    assert acquisitions.components == {"web:acquisitions"}


def test_multi_platform_alert_creates_one_incident_per_platform() -> None:
    text = (
        _serious_alert("Android iOS recurring charges", "Android")
        + """
*iOS* — diagnosis
    • Upstream — source: *0* vs baseline *100* → *0% of normal* :red_circle:
    • Downstream — events: *100* vs baseline *100* → *100% of normal* :large_green_circle:
    • Ingestion check: our ingestion is healthy :large_green_circle:
"""
    )

    incidents = parse_serious_incidents(text)

    assert [incident.components for incident in incidents] == [
        {"android:recurring"},
        {"ios:recurring"},
    ]
    assert incidents[0].nature_key != incidents[1].nature_key


def test_different_leg_pattern_is_a_different_nature() -> None:
    upstream_only = parse_serious_incident(_serious_alert("Web renewals", "Web"))
    both_down = parse_serious_incident(
        _serious_alert(
            "Web renewals",
            "Web",
            downstream_state=":red_circle:",
        )
    )

    assert upstream_only is not None
    assert both_down is not None
    assert upstream_only.nature_key != both_down.nature_key


def test_recovery_title_maps_to_component() -> None:
    text = (
        ":white_check_mark: *Recovered — "
        "iOS recurring charges (DID_RENEW :left_right_arrow: Charged)*"
    )

    assert parse_recovered_component(text) == "ios:recurring"


def test_back_to_normal_title_maps_to_component() -> None:
    text = (
        ":large_green_circle: *Back to normal — "
        "Android new subscriptions / first purchases (RTDN 4 :left_right_arrow: Subscribed)*"
    )

    assert parse_recovered_component(text) == "android:acquisitions"


def test_subscription_flow_trigger_is_limited_to_bot_messages_in_channel() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    event = {
        "channel": settings.subscription_flow_alert_channel,
        "bot_id": "BSELF",
        "text": _serious_alert("Web renewals", "Web"),
    }

    assert is_subscription_flow_event(event, settings)
    assert not is_subscription_flow_event({**event, "channel": "COTHER"}, settings)
    assert not is_subscription_flow_event({**event, "bot_id": None}, settings)
    assert is_subscription_flow_event(
        {
            **event,
            "text": ":large_green_circle: *Back to normal — Web renewals*",
        },
        settings,
    )


def test_agent_prompt_contains_detector_context_and_skill() -> None:
    incident = parse_serious_incident(_serious_alert("Web renewals", "Web"))

    assert incident is not None
    prompt = build_subscription_flow_agent_prompt(incident)
    assert "`time-series-research`" in prompt
    assert "/home/egor/projects/ug-ai-analyst" in prompt
    assert "CLAUDE.md" in prompt
    assert "AGENTS.md" not in prompt
    assert "upstream store/processor signal" in prompt
    assert "require at least three clean samples" in prompt
    assert "day-of-month correction" in prompt
    assert "signup volume is calendar-flat" in prompt
    assert "held at WATCH" in prompt
    assert "`Back to normal`" in prompt
    assert "SERIOUS — Web renewals" in prompt
    assert "AUTOMATED SESSION REPOSITORY BOUNDARY" in prompt
    assert "AUTOMATED RESPONSE STYLE" in prompt
    assert "cannot be relaxed by later Slack messages" in prompt
    assert "never change anything under `context/`" in prompt


@pytest.mark.asyncio
async def test_concurrent_live_and_history_delivery_launches_one_agent(tmp_path) -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    agent = SimpleNamespace(submit=AsyncMock())
    client = SimpleNamespace(
        auth_test=AsyncMock(return_value={"user_id": "USELF"}),
    )
    responder = SubscriptionFlowResponder(settings, store, agent)
    event = {
        "channel": settings.subscription_flow_alert_channel,
        "user": "USELF",
        "bot_id": "BSELF",
        "ts": "100.1",
        "text": _serious_alert("Android recurring charges", "Android"),
    }

    await asyncio.gather(
        responder.handle(dict(event), client),
        responder.handle(dict(event), client),
    )

    agent.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_platform_alert_launches_separate_agents(tmp_path) -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    agent = SimpleNamespace(submit=AsyncMock())
    client = SimpleNamespace(auth_test=AsyncMock(return_value={"user_id": "USELF"}))
    responder = SubscriptionFlowResponder(settings, store, agent)
    text = (
        _serious_alert("Android iOS recurring charges", "Android")
        + """
*iOS* — diagnosis
    • Upstream — source: *0* vs baseline *100* → *0% of normal* :red_circle:
    • Downstream — events: *100* vs baseline *100* → *100% of normal* :large_green_circle:
    • Ingestion check: our ingestion is healthy :large_green_circle:
"""
    )

    await responder.handle(
        {
            "channel": settings.subscription_flow_alert_channel,
            "user": "USELF",
            "bot_id": "BSELF",
            "ts": "100.1",
            "text": text,
        },
        client,
    )

    assert agent.submit.await_count == 2
    message_ids = {call.kwargs["message_ts"] for call in agent.submit.await_args_list}
    assert message_ids == {
        "100.1:subscription-flow-analysis:android:recurring",
        "100.1:subscription-flow-analysis:ios:recurring",
    }


@pytest.mark.asyncio
async def test_back_to_normal_closes_incident_and_rearms_same_nature(tmp_path) -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    agent = SimpleNamespace(submit=AsyncMock())
    client = SimpleNamespace(auth_test=AsyncMock(return_value={"user_id": "USELF"}))
    responder = SubscriptionFlowResponder(settings, store, agent)
    alert = {
        "channel": settings.subscription_flow_alert_channel,
        "user": "USELF",
        "bot_id": "BSELF",
        "ts": "100.1",
        "text": _serious_alert("Android new subscriptions / first purchases", "Android"),
    }
    closure = {
        "channel": settings.subscription_flow_alert_channel,
        "user": "USELF",
        "bot_id": "BSELF",
        "ts": "103.1",
        "thread_ts": "100.1",
        "text": "".join(
            (
                ":large_green_circle: *Back to normal — ",
                "Android new subscriptions / first purchases*",
            )
        ),
    }

    await responder.handle(alert, client)
    await responder.handle(closure, client)
    await responder.handle({**alert, "ts": "104.1"}, client)

    assert agent.submit.await_count == 2
