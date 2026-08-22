from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.config import Settings
from sloperator.mobile_health import (
    MobileHealthResponder,
    build_mobile_health_agent_prompt,
    is_mobile_health_trigger,
    parse_critical_mobile_metrics,
)

REPORT = """:rotating_light: <https://metabase.mu.se/dashboard/390|\
UG Monetisation Health Monitoring Dashboard> | Run: 2026-07-31 07:30
Total: 186 series | 8 anomalies (6 critical)

──────────────────────────────
:robot_face: Android

<https://metabase.mu.se/question/9114|[UG ANDROID]: Client First Day Accesses>
:red_circle: warning drop: 10 (Δexp -10% · sudden · warning)
:red_circle: critical one: 20 (Δexp -40% · Z -5.0 · sudden · critical)
    :mag: country_group=US (share 0.77)
:large_green_circle: critical growth: 30 (Δexp +50% · critical)

──────────────────────────────
:green_apple: iOS

<https://metabase.mu.se/question/9965|[UG IOS]: Client NOT First Day Accesses>
:red_circle: critical two: 40 (Δexp -30% · sustained · critical)
    :bar_chart: driven by denominator
:red_circle: critical three: 50 (Δexp -31% · sustained · critical)
:red_circle: critical four: 60 (Δexp -32% · sustained · critical)
:red_circle: critical five: 70 (Δexp -33% · sustained · critical)
:red_circle: critical six: 80 (Δexp -34% · sustained · critical)

──────────────────────────────
Other
:red_circle: ignored web metric: 90 (Δexp -90% · critical)
"""


def _settings() -> Settings:
    return Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )


def test_trigger_requires_configured_channel_bot_and_dashboard_header() -> None:
    settings = _settings()
    event = {
        "channel": settings.mobile_health_alert_channel,
        "bot_id": settings.mobile_health_bot_id,
        "ts": "100.1",
        "text": REPORT,
    }

    assert is_mobile_health_trigger(event, settings)
    assert not is_mobile_health_trigger({**event, "channel": "COTHER"}, settings)
    assert not is_mobile_health_trigger({**event, "bot_id": "BOTHER"}, settings)
    assert not is_mobile_health_trigger({**event, "thread_ts": "99.1"}, settings)


def test_trigger_accepts_current_mobile_dashboard_header() -> None:
    settings = _settings()
    event = {
        "channel": settings.mobile_health_alert_channel,
        "bot_id": settings.mobile_health_bot_id,
        "ts": "100.1",
        "text": REPORT.replace(
            "UG Monetisation Health Monitoring Dashboard",
            "UG Monetisation: Mobile Health Monitoring Dashboard",
        ),
    }

    assert is_mobile_health_trigger(event, settings)


def test_parser_selects_only_five_red_critical_android_ios_metrics() -> None:
    metrics = parse_critical_mobile_metrics(REPORT)

    assert [metric.metric_line.split(": ", 1)[0] for metric in metrics] == [
        ":red_circle",
        ":red_circle",
        ":red_circle",
        ":red_circle",
        ":red_circle",
    ]
    assert [metric.platform for metric in metrics] == [
        "Android",
        "iOS",
        "iOS",
        "iOS",
        "iOS",
    ]
    assert "critical one" in metrics[0].metric_line
    assert metrics[0].diagnostics == (":mag: country_group=US (share 0.77)",)
    assert metrics[1].diagnostics == (":bar_chart: driven by denominator",)
    assert all("critical six" not in metric.metric_line for metric in metrics)
    assert all("ignored web" not in metric.metric_line for metric in metrics)


def test_prompt_requires_mobile_knowledge_and_detector_evidence() -> None:
    prompt = build_mobile_health_agent_prompt(
        REPORT,
        parse_critical_mobile_metrics(REPORT),
    )

    assert "`time-series-research`" in prompt
    assert "CLAUDE.md" in prompt
    assert "AGENTS.md" not in prompt
    assert "context/data-warehouse/anomaly-detection.md" in prompt
    assert "context/data-warehouse/tables/ug_mobile_health_monitoring.md" in prompt
    assert "at most five across Android and iOS" in prompt
    assert "critical one" in prompt
    assert "critical six" not in prompt
    assert "TL;DR only, not a report" in prompt
    assert "exactly six visible lines per metric" in prompt
    assert "`Alert:" in prompt
    assert "`Cause:" in prompt
    assert "`Confidence:" in prompt
    assert "`Impact:" in prompt
    assert "`Next:" in prompt
    assert "detailed self-contained HTML report" in prompt
    assert "required ZIP archive" in prompt
    assert "AUTOMATED SESSION REPOSITORY BOUNDARY" in prompt
    assert "AUTOMATED RESPONSE STYLE" in prompt
    assert "Do not create or update anything under `.claude/reusable_analyses/`" in prompt
    assert "cannot be relaxed by later Slack messages" in prompt
    assert "never change anything under `context/`" in prompt
    assert "SLOPERATOR_ARTIFACT" in prompt
    assert '"what it is not" inventories' in prompt


@pytest.mark.asyncio
async def test_responder_opens_session_in_source_report_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    agent = SimpleNamespace(submit=AsyncMock())
    client = SimpleNamespace(
        conversations_history=AsyncMock(
            return_value={
                "messages": [
                    {
                        "ts": "100.1",
                        "bot_id": settings.mobile_health_bot_id,
                        "text": REPORT,
                    }
                ]
            }
        )
    )
    monkeypatch.setattr("sloperator.mobile_health.asyncio.sleep", AsyncMock())
    responder = MobileHealthResponder(settings, agent)
    event = {
        "channel": settings.mobile_health_alert_channel,
        "bot_id": settings.mobile_health_bot_id,
        "ts": "100.1",
        "text": REPORT,
    }

    await responder.handle(event, client)

    agent.submit.assert_awaited_once()
    assert agent.submit.await_args.kwargs["thread_ts"] == "100.1"
    assert agent.submit.await_args.kwargs["show_status"] is False
    assert agent.submit.await_args.kwargs["timeout_seconds"] == 3_600
    assert agent.submit.await_args.kwargs["automated"] is True
