from __future__ import annotations

import asyncio
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from sloperator.anomaly_alerts import (
    TABLE_MAX_ROWS,
    Alert,
    AlertBatch,
    AnomalyAlertResponder,
    build_batch_sql,
    build_batches,
    build_monetisation_agent_prompt,
    confirmed_monetisation_anomalies,
    is_anomaly_trigger,
    render_fallback_text,
    render_messages,
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
    assert "CLAUDE.md" in prompt
    assert "AGENTS.md" not in prompt
    assert "Landing Purchase" in prompt
    assert "usual useful charts and visual-review workflow" in prompt
    assert "only producing non-critical cosmetic changes" in prompt
    assert "`.claude/reusable_analyses/README.md`" in prompt
    assert "previous five days" in prompt
    assert "`metric + platform + type` identities" in prompt
    assert "SLOPERATOR_REUSE_ANALYSIS:" in prompt
    assert "return exactly this one line" in prompt
    assert "Do not add a case or update the index" in prompt
    assert "unless a human explicitly asks" in prompt
    assert "AUTOMATED SESSION REPOSITORY BOUNDARY" in prompt
    assert "AUTOMATED RESPONSE STYLE" in prompt
    assert "cannot be relaxed by later Slack messages" in prompt
    assert "never change anything under `context/`" in prompt


def _big_batch(size: int) -> tuple[AlertBatch, list[dict[str, Any]]]:
    alerts = [
        Alert(f"Metric {index}", "ios", "events", "+20", 120, 100, "0.001")
        for index in range(size)
    ]
    batch = AlertBatch("2026-10-13 21:00:00", "100.1", alerts=alerts)
    results = [
        {"status": "ok", "verdict": "ANOMALY", "value": 120, "last_week": 100,
         "wow": 0.2, "peak_wow": 0.2}
        for _ in alerts
    ]
    return batch, results


def test_large_batch_is_split_into_messages_with_one_table_each() -> None:
    batch, results = _big_batch(221)
    results[-1] = {"status": "ok", "verdict": "OK"}

    messages = render_messages(batch, results, 0.10)

    assert len(messages) == 3
    data_rows = 0
    for _, blocks in messages:
        tables = [block for block in blocks if block["type"] == "table"]
        assert len(tables) == 1
        assert len(tables[0]["rows"]) <= TABLE_MAX_ROWS
        data_rows += len(tables[0]["rows"]) - 1
    assert data_rows == 220
    assert "Part 1/3" in messages[0][0]
    assert "220 real anomaly(ies)" in messages[0][0]
    assert "Other 1 metric(s) omitted" in str(messages[-1][1][-1])


def test_small_batch_stays_a_single_message() -> None:
    batch, results = _big_batch(3)

    messages = render_messages(batch, results, 0.10)

    assert len(messages) == 1
    assert "Part" not in messages[0][0]


def test_fallback_text_is_capped() -> None:
    batch, results = _big_batch(50)

    text = render_fallback_text(batch, results, 0.10)

    assert "…and 20 more." in text
    assert "rejected by Slack" in text


class _FakeClient:
    def __init__(self, fail_on_blocks: bool) -> None:
        self.fail_on_blocks = fail_on_blocks
        self.calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail_on_blocks and "blocks" in kwargs:
            raise SlackApiError("invalid_blocks", {"ok": False, "error": "invalid_blocks"})
        return {"ok": True}


def _responder() -> AnomalyAlertResponder:
    responder = AnomalyAlertResponder.__new__(AnomalyAlertResponder)
    responder.settings = _settings()
    return responder


def test_post_reply_sends_every_part_in_thread() -> None:
    batch, results = _big_batch(150)
    client = _FakeClient(fail_on_blocks=False)

    asyncio.run(_responder()._post_reply(client, "100.1", batch, results))  # type: ignore[arg-type]

    assert len(client.calls) == 2
    assert all(call["thread_ts"] == "100.1" for call in client.calls)


def test_post_reply_falls_back_to_text_when_slack_rejects_blocks() -> None:
    batch, results = _big_batch(150)
    client = _FakeClient(fail_on_blocks=True)

    asyncio.run(_responder()._post_reply(client, "100.1", batch, results))  # type: ignore[arg-type]

    assert "blocks" not in client.calls[-1]
    assert "rejected by Slack" in client.calls[-1]["text"]
