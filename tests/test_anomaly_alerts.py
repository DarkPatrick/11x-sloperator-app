from __future__ import annotations

import pytest

from sloperator.anomaly_alerts import (
    Alert,
    AlertBatch,
    build_batch_sql,
    build_batches,
    build_monetisation_agent_prompt,
    confirmed_monetisation_anomalies,
    is_anomaly_trigger,
    verdict_for_metric,
)
from sloperator.config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "slack_user_id": "UOPERATOR",
        "bot_token": "xoxb-test",
        "app_token": "xapp-test",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_trigger_requires_channel_bot_and_exact_operator_mention() -> None:
    settings = _settings()
    event = {
        "channel": settings.anomaly_alert_channel,
        "bot_id": settings.anomaly_bot_id,
        "text": "please check <@UOPERATOR>",
    }

    assert is_anomaly_trigger(event, settings)
    assert not is_anomaly_trigger({**event, "channel": "COTHER"}, settings)
    assert not is_anomaly_trigger({**event, "bot_id": "BOTHER"}, settings)
    assert not is_anomaly_trigger({**event, "text": "please check <@UOTHER>"}, settings)


def test_build_batches_reassembles_split_alert_and_mention_messages() -> None:
    messages = [
        (
            "100.1",
            "Found anomalies in project *UG* for *2026-07-25 10:00:00 (UTC)*\n"
            "*Tab View*, *ios*, change in *events* +17.40% | "
            "was: 87135, expected: 74221 | p-value: 0.000002",
        ),
        ("100.2", "please check <@UOPERATOR>"),
    ]

    batches = build_batches(messages, "UOPERATOR")

    assert len(batches) == 1
    assert batches[0].alert_dt == "2026-07-25 10:00:00"
    assert batches[0].mention_ts == "100.2"
    assert batches[0].alerts[0].key() == ("Tab View", "ios", "events")


def test_verdict_matches_peak_week_over_week_rule() -> None:
    settings = _settings(anomaly_threshold=0.10)
    alert = Alert("Metric", "ios", "events", "+20", 120, 100, "0.001")
    rows = [
        {
            "datetime": "2026-07-25 10:00:00",
            "metric": "Metric",
            "platform": "ios",
            "metric_type": "events",
            "value": 120,
            "prediction": 100,
        },
        {
            "datetime": "2026-07-18 10:00:00",
            "metric": "Metric",
            "platform": "ios",
            "metric_type": "events",
            "value": 100,
            "prediction": 100,
        },
    ]

    result = verdict_for_metric(rows, alert, "2026-07-25 10:00:00", settings)

    assert result["verdict"] == "ANOMALY"
    assert result["wow"] == pytest.approx(0.2)


def test_batch_sql_escapes_dimension_values() -> None:
    batch = AlertBatch(
        "2026-07-25 10:00:00",
        "100.1",
        alerts=[Alert("Bob's metric", "ios", "events", "+20", 120, 100, "0.001")],
    )

    sql = build_batch_sql(batch, _settings())

    assert "Bob''s metric" in sql
    assert "format JSON" in sql


def test_only_confirmed_monetisation_anomalies_launch_analysis() -> None:
    batch = AlertBatch(
        "2026-07-25 10:00:00",
        "100.1",
        alerts=[
            Alert("Landing Purchase", "web", "events", "-20", 80, 100, "0.001"),
            Alert("Tab View 60s", "ios", "events", "-30", 70, 100, "0.001"),
            Alert("Splash View", "ios", "events", "+5", 105, 100, "0.001"),
        ],
    )
    results = [
        {"status": "ok", "verdict": "ANOMALY"},
        {"status": "ok", "verdict": "ANOMALY"},
        {"status": "ok", "verdict": "OK"},
    ]

    selected = confirmed_monetisation_anomalies(batch, results)

    assert [alert.metric for alert, _ in selected] == ["Landing Purchase"]


def test_monetisation_agent_prompt_requires_time_series_skill() -> None:
    batch = AlertBatch("2026-07-25 10:00:00", "100.1")
    anomaly = Alert("Landing Purchase", "web", "events", "-20", 80, 100, "0.001")

    prompt = build_monetisation_agent_prompt(
        batch,
        [
            (
                anomaly,
                {
                    "status": "ok",
                    "verdict": "ANOMALY",
                    "value": 80,
                    "last_week": 100,
                    "wow": -0.2,
                    "peak_wow": -0.25,
                },
            )
        ],
    )

    assert "`time-series-research`" in prompt
    assert "/home/egor/projects/ug-ai-analyst" in prompt
    assert "Landing Purchase" in prompt
