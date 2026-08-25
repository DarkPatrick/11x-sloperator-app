from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.config import Settings
from sloperator.web_health import (
    WebHealthResponder,
    build_web_health_agent_prompt,
    is_web_health_trigger,
    parse_critical_web_metrics,
)

REPORT = """:rotating_light: <https://metabase.mu.se/dashboard/104|\
UG Monetisation: WEB health monitoring> | Run: 2026-08-25 07:30
Other: 80 series | :red_circle: 7 drops, :large_green_circle: 1 growth
Total: 80 series | 8 anomalies (6 critical)

──────────────────────────────
Other

:rotating_light: <https://metabase.mu.se/question/5342|[UGM] WEB: accesses by source - rt>
:red_circle: warning drop: 10 (Δexp -10% · sudden · warning)
:red_circle: critical one: 20 (Δexp -40% · Z -5.0 · sudden · critical)
    :mag: funnel_source=paid (share 0.77)
:large_green_circle: critical growth: 30 (Δexp +50% · critical)

<https://metabase.mu.se/question/6909|[UGM WEB]: Trial -> charge conversion, rt>
:red_circle: critical two: 40 (Δexp -30% · sustained · critical)
    :bar_chart: driven by denominator
:red_circle: critical three: 50 (Δexp -31% · sustained · critical)
:red_circle: critical four: 60 (Δexp -32% · sustained · critical)
:red_circle: critical five: 70 (Δexp -33% · sustained · critical)
:red_circle: critical six: 80 (Δexp -34% · sustained · critical)
"""


def _settings() -> Settings:
    return Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")


def test_trigger_requires_configured_source_and_web_header() -> None:
    settings = _settings()
    event = {
        "channel": settings.mobile_health_alert_channel,
        "bot_id": settings.mobile_health_bot_id,
        "ts": "100.1",
        "text": REPORT,
    }

    assert is_web_health_trigger(event, settings)
    assert not is_web_health_trigger({**event, "channel": "COTHER"}, settings)
    assert not is_web_health_trigger({**event, "bot_id": "BOTHER"}, settings)
    assert not is_web_health_trigger({**event, "thread_ts": "99.1"}, settings)
    assert not is_web_health_trigger(
        {
            **event,
            "text": REPORT.replace(
                "WEB health monitoring", "Mobile Health Monitoring Dashboard"
            ),
        },
        settings,
    )


def test_parser_selects_only_five_red_critical_web_metrics() -> None:
    metrics = parse_critical_web_metrics(REPORT)

    assert len(metrics) == 5
    assert "critical one" in metrics[0].metric_line
    assert "critical five" in metrics[-1].metric_line
    assert all("warning drop" not in metric.metric_line for metric in metrics)
    assert all("critical growth" not in metric.metric_line for metric in metrics)
    assert all("critical six" not in metric.metric_line for metric in metrics)
    assert metrics[0].diagnostics == (":mag: funnel_source=paid (share 0.77)",)
    assert metrics[1].diagnostics == (":bar_chart: driven by denominator",)


def test_prompt_uses_web_dashboard_knowledge_and_automated_style() -> None:
    prompt = build_web_health_agent_prompt(REPORT, parse_critical_web_metrics(REPORT))

    assert "`time-series-research`" in prompt
    assert "context/data-warehouse/anomaly-detection.md" in prompt
    assert "context/data-warehouse/dashboards/ug_web_health_monitoring.md" in prompt
    assert "dashboard 104 has no shared datamart" in prompt.lower()
    assert "critical one" in prompt
    assert "critical six" not in prompt
    assert "TL;DR only, not a report" in prompt
    assert "exactly six visible lines per metric" in prompt
    assert "AUTOMATED SESSION REPOSITORY BOUNDARY" in prompt
    assert "AUTOMATED RESPONSE STYLE" in prompt
    assert "SLOPERATOR_ARTIFACT" in prompt
    assert "<@U0149RHN7D3> <@U09CYCGN6H4> <@U0525MDT0MN>" in prompt


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
    monkeypatch.setattr("sloperator.web_health.asyncio.sleep", AsyncMock())
    responder = WebHealthResponder(settings, agent)

    await responder.handle(
        {
            "channel": settings.mobile_health_alert_channel,
            "bot_id": settings.mobile_health_bot_id,
            "ts": "100.1",
            "text": REPORT,
        },
        client,
    )

    agent.submit.assert_awaited_once()
    assert agent.submit.await_args.kwargs["thread_ts"] == "100.1"
    assert agent.submit.await_args.kwargs["show_status"] is False
    assert agent.submit.await_args.kwargs["require_artifact"] is True
    assert agent.submit.await_args.kwargs["timeout_seconds"] == 3_600
    assert agent.submit.await_args.kwargs["automated"] is True
