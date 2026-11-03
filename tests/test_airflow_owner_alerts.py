from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sloperator.airflow_owner_alerts import (
    AirflowOwnerResponder,
    FailedTask,
    is_airflow_owner_trigger,
    parse_alert,
    reply_text,
)
from sloperator.config import Settings

ALERT = """:x: DAG Failed
*Dag*: ug_google_play_reports
*Task*: load
*Logical Date*: 2026-10-28 12:00:00
*<https://airflow-web.an.k8s.mu.se/dags/ug_google_play_reports/runs/\
scheduled__2026-10-28T12:00:00+00:00/tasks/load|Logs>*"""
CRITICAL = """:fire::fire::fire: Critical DAG Failed <!subteam^S082GN2MVK5> :fire::fire::fire:
*Dag*: ug_analytics_olap_daily
*Task*: ug_olap_staged_daily
*Logical Date*: 2026-11-02 01:30:00
*<https://airflow-web.an.k8s.mu.se/dags/ug_analytics_olap_daily/runs/\
scheduled__2026-11-02T01:30:00%2B00:00/tasks/ug_olap_staged_daily|Logs>*"""
DATA_LOSS = "Last hour data loss. Check next tables please:\n\n* ug_shot_events"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        database_path=tmp_path / "sloperator.sqlite3",
    )


def _event(settings: Settings, text: str = ALERT, **overrides: object) -> dict[str, object]:
    return {
        "channel": settings.airflow_alert_channel,
        "bot_id": settings.anomaly_bot_id,
        "ts": f"{time.time():.6f}",
        "text": text,
        **overrides,
    }


def test_parse_alert_reads_dag_run_and_task() -> None:
    assert parse_alert(ALERT) == FailedTask(
        "ug_google_play_reports", "scheduled__2026-10-28T12:00:00+00:00", "load"
    )
    assert parse_alert(CRITICAL) == FailedTask(
        "ug_analytics_olap_daily", "scheduled__2026-11-02T01:30:00+00:00", "ug_olap_staged_daily"
    )
    assert parse_alert(DATA_LOSS) is None


def test_trigger_matches_only_top_level_bot_alerts_in_the_airflow_channel(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    event = _event(settings)

    assert is_airflow_owner_trigger(event, settings)
    assert is_airflow_owner_trigger({**event, "bot_id": None, "user": "U018X57PTFV"}, settings)
    assert is_airflow_owner_trigger({**event, "thread_ts": event["ts"]}, settings)
    assert not is_airflow_owner_trigger({**event, "thread_ts": "1.0"}, settings)
    assert not is_airflow_owner_trigger({**event, "channel": "COTHER"}, settings)
    assert not is_airflow_owner_trigger({**event, "bot_id": "BOTHER"}, settings)
    assert not is_airflow_owner_trigger({**event, "text": DATA_LOSS}, settings)


def test_reply_text_names_the_reason() -> None:
    dbt = reply_text("UOWNER", {"kind": "dbt", "owned_models": ["int__ug_refunds_ios"]})
    plain = reply_text("UOWNER", {"kind": "plain", "access_roles": ["User_UG_Monetisation"]})

    assert dbt.startswith("<@UOWNER>") and "`int__ug_refunds_ios`" in dbt
    assert plain.startswith("<@UOWNER>") and "User_UG_Monetisation" in plain


@pytest.mark.asyncio
async def test_mentions_in_thread_once_when_the_alert_is_the_operators(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    responder = AirflowOwnerResponder(settings)
    responder.resolve = AsyncMock(  # type: ignore[method-assign]
        return_value={"mention": True, "kind": "plain", "access_roles": ["User_UG_Monetisation"]}
    )
    client = AsyncMock()
    event = _event(settings)

    await responder.handle(event, client)
    await responder.handle(event, client)

    client.chat_postMessage.assert_awaited_once()
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["thread_ts"] == event["ts"]
    assert kwargs["channel"] == settings.airflow_alert_channel
    assert "<@UOWNER>" in kwargs["text"]
    responder.resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_posts_nothing_for_someone_elses_alert(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    responder = AirflowOwnerResponder(settings)
    responder.resolve = AsyncMock(  # type: ignore[method-assign]
        return_value={"mention": False, "kind": "dbt", "owned_models": []}
    )
    client = AsyncMock()

    await responder.handle(_event(settings), client)

    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_alerts_replayed_from_old_history(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    responder = AirflowOwnerResponder(settings)
    responder.resolve = AsyncMock()  # type: ignore[method-assign]
    client = AsyncMock()

    await responder.handle(_event(settings, ts=f"{time.time() - 7 * 3_600:.6f}"), client)

    responder.resolve.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_resolution_is_not_marked_handled(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    responder = AirflowOwnerResponder(settings)
    responder.resolve = AsyncMock(side_effect=RuntimeError("bridge offline"))  # type: ignore[method-assign]
    client = AsyncMock()
    event = _event(settings)

    await responder.handle(event, client)

    client.chat_postMessage.assert_not_awaited()
    assert not responder._handled(str(event["ts"]))
