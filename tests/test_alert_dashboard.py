from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sloperator.alert_dashboard import (
    AlertDashboardResponder,
    PublishedDashboard,
    announcement,
    build_dump,
    collect_reports,
    complete_run,
    count_red_alerts,
    is_alert_dashboard_trigger,
    is_top_level,
    report_marker,
    run_date,
)
from sloperator.config import Settings

WEB_REPORT = """:rotating_light: <https://metabase.mu.se/dashboard/104|\
UG Monetisation: WEB health monitoring> | Run: 2026-09-17 07:27
Other: 80 series | :red_circle: 2 drops, :large_green_circle: 1 growth

──────────────────────────────
Other

<https://metabase.mu.se/question/5342|[UGM] WEB: accesses by source - rt>
:red_circle: web drop one: 10 (Δexp -10.0% · sudden · warning)
:large_green_circle: web growth: 30 (Δexp +50.0% · critical)
"""
WEB_CHUNK = """<https://metabase.mu.se/question/6909|[UGM WEB]: Trial -> charge conversion, rt>
:red_circle: web drop two: 40 (Δexp -30.0% · sustained · critical)
"""
MOBILE_REPORT = """:rotating_light: <https://metabase.mu.se/dashboard/390|\
UG Monetisation: Mobile Health Monitoring Dashboard> | Run: 2026-09-17 07:30
Total: 40 series | 2 anomalies (1 critical)

──────────────────────────────
:green_apple: iOS

<https://metabase.mu.se/question/9963|[UGM IOS]: Client First Day Accesses>
:red_circle: Autoscroll | access_cnt: 7 (Δexp -61.6% · sudden · warning)
"""
SUBSCRIPTIONS_REPORT = """:rotating_light: <https://metabase.mu.se/dashboard/476|\
UG Monetisation: WEB Subscriptions Monitoring> | Run: 2026-09-17 07:37
Total: 71 series | 3 anomalies (0 critical)

──────────────────────────────
Other

<https://metabase.mu.se/question/11968|[UG WEB] CR from Checkout View to Purchase Success>
:red_circle: cr from checkout view to purchase: 32.73 (Δexp -9.8% · sudden · warning)
"""
UNRELATED = "Deploy finished for build 7.3.19\n"


def _settings(**overrides: object) -> Settings:
    return Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        **overrides,
    )


def _run_messages() -> list[tuple[str, str]]:
    return [
        ("1789629900.000100", WEB_REPORT),
        ("1789629902.000100", WEB_CHUNK),
        ("1789630245.000100", MOBILE_REPORT),
        ("1789630654.000100", SUBSCRIPTIONS_REPORT),
    ]


def test_trigger_matches_each_report_header_from_the_configured_webhook() -> None:
    settings = _settings()
    event = {
        "channel": settings.mobile_health_alert_channel,
        "bot_id": settings.mobile_health_bot_id,
        "ts": "1789630654.000100",
        "text": SUBSCRIPTIONS_REPORT,
    }

    assert is_alert_dashboard_trigger(event, settings)
    assert is_alert_dashboard_trigger({**event, "text": WEB_REPORT}, settings)
    assert is_alert_dashboard_trigger({**event, "text": MOBILE_REPORT}, settings)
    assert not is_alert_dashboard_trigger({**event, "channel": "COTHER"}, settings)
    assert not is_alert_dashboard_trigger({**event, "bot_id": "BOTHER"}, settings)
    assert not is_alert_dashboard_trigger({**event, "thread_ts": "1789630000.1"}, settings)
    # A report the health triggers already answered is a thread parent, not a reply.
    assert is_alert_dashboard_trigger({**event, "thread_ts": event["ts"]}, settings)
    assert not is_alert_dashboard_trigger({**event, "text": WEB_CHUNK}, settings)
    assert not is_alert_dashboard_trigger({**event, "text": UNRELATED}, settings)


def test_report_marker_identifies_only_headers() -> None:
    assert report_marker(WEB_REPORT) == "UG Monetisation: WEB health monitoring"
    assert report_marker(MOBILE_REPORT) == "UG Monetisation: Mobile Health Monitoring Dashboard"
    assert report_marker(SUBSCRIPTIONS_REPORT) == "UG Monetisation: WEB Subscriptions Monitoring"
    assert report_marker(WEB_CHUNK) is None


def test_collect_reports_attaches_continuation_chunks_to_their_header() -> None:
    reports = collect_reports(_run_messages())

    assert set(reports) == {
        "UG Monetisation: WEB health monitoring",
        "UG Monetisation: Mobile Health Monitoring Dashboard",
        "UG Monetisation: WEB Subscriptions Monitoring",
    }
    web = reports["UG Monetisation: WEB health monitoring"]
    assert "web drop two" in web.text
    assert web.ts == "1789629900.000100"
    assert "web drop two" not in reports["UG Monetisation: WEB Subscriptions Monitoring"].text


def test_collect_reports_ignores_a_chunk_posted_long_after_its_header() -> None:
    late = [("1789629900.000100", WEB_REPORT), ("1789629999.000100", WEB_CHUNK)]

    reports = collect_reports(late)

    assert "web drop two" not in reports["UG Monetisation: WEB health monitoring"].text


def test_collect_reports_keeps_the_most_recent_run_of_a_repeated_report() -> None:
    rerun = WEB_REPORT.replace("07:27", "11:27").replace("web drop one", "rerun drop")
    reports = collect_reports([*_run_messages(), ("1789645000.000100", rerun)])

    assert "rerun drop" in reports["UG Monetisation: WEB health monitoring"].text


def test_complete_run_requires_all_three_reports_within_one_window() -> None:
    assert complete_run(collect_reports(_run_messages())) is not None
    assert complete_run(collect_reports(_run_messages()[:3])) is None

    stale = [("1789600000.000100", WEB_REPORT), *_run_messages()[2:]]
    assert complete_run(collect_reports(stale)) is None


def test_build_dump_preserves_channel_order_and_report_text() -> None:
    dump = build_dump(collect_reports(_run_messages()))

    assert dump.index("WEB health monitoring") < dump.index("Mobile Health Monitoring")
    assert dump.index("Mobile Health Monitoring") < dump.index("WEB Subscriptions Monitoring")
    assert dump.endswith("\n")


def test_count_red_alerts_and_run_date_read_the_detector_grammar() -> None:
    dump = build_dump(collect_reports(_run_messages()))

    assert count_red_alerts(dump) == 4
    assert run_date(dump) == "2026-09-17"
    assert count_red_alerts(UNRELATED) == 0
    assert run_date(UNRELATED) is None


def test_announcement_is_one_line_with_the_link_behind_the_opening_phrase() -> None:
    published = PublishedDashboard("https://metabase.mu.se/dashboard/539", ("Web", "iOS"), 4)

    text = announcement(published, "2026-09-17")

    assert text == (
        ":bar_chart: <https://metabase.mu.se/dashboard/539|Свежий дашборд> "
        "за 2026-09-17 по аномалиям собран"
    )
    assert "\n" not in text


def test_announcement_omits_an_unknown_run_date() -> None:
    published = PublishedDashboard("https://metabase.mu.se/dashboard/539", (), 0)

    assert announcement(published, None) == (
        ":bar_chart: <https://metabase.mu.se/dashboard/539|Свежий дашборд> по аномалиям собран"
    )


def _responder(tmp_path: Path) -> AlertDashboardResponder:
    return AlertDashboardResponder(_settings(database_path=tmp_path / "sloperator.sqlite3"))


def _history_client(messages: list[tuple[str, str]], bot_id: str) -> AsyncMock:
    client = AsyncMock()
    client.conversations_history.return_value = {
        # Slack stamps `thread_ts` on a report once a health trigger has replied to it.
        "messages": [
            {"ts": ts, "text": text, "bot_id": bot_id, "thread_ts": ts}
            for ts, text in messages
        ]
    }
    return client


def test_is_top_level_keeps_thread_parents_and_drops_replies() -> None:
    assert is_top_level({"ts": "100.1"})
    assert is_top_level({"ts": "100.1", "thread_ts": "100.1"})
    assert not is_top_level({"ts": "100.2", "thread_ts": "100.1"})


@pytest.mark.asyncio
async def test_handle_rebuilds_once_when_the_last_report_of_a_run_arrives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responder = _responder(tmp_path)
    monkeypatch.setattr("sloperator.alert_dashboard.CHUNK_SETTLE_SECONDS", 0)
    published = PublishedDashboard("https://metabase.mu.se/dashboard/539", ("Web", "iOS"), 4)
    calls: list[tuple[str, str]] = []

    async def fake_rebuild(dump: str, date: str) -> PublishedDashboard:
        calls.append((dump, date))
        return published

    monkeypatch.setattr(responder, "rebuild", fake_rebuild)
    client = _history_client(_run_messages(), responder.settings.mobile_health_bot_id)
    event = {
        "channel": responder.settings.mobile_health_alert_channel,
        "bot_id": responder.settings.mobile_health_bot_id,
        "ts": "1789630654.000100",
        "text": SUBSCRIPTIONS_REPORT,
    }

    await responder.handle(event, client)

    assert len(calls) == 1
    dump, date = calls[0]
    assert date == "2026-09-17"
    assert "web drop two" in dump
    posted = client.chat_postMessage.await_args.kwargs
    assert posted["channel"] == responder.settings.mobile_health_alert_channel
    assert "thread_ts" not in posted
    assert "dashboard/539" in posted["text"]

    # The recorded run must not be rebuilt again after a restart or a redelivered event.
    await responder.handle(event, client)
    assert len(calls) == 1
    assert json.loads(responder.state_path.read_text())["run_ts"] == "1789630654.000100"


@pytest.mark.asyncio
async def test_handle_waits_for_a_report_that_has_not_arrived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responder = _responder(tmp_path)
    monkeypatch.setattr("sloperator.alert_dashboard.CHUNK_SETTLE_SECONDS", 0)
    monkeypatch.setattr(responder, "rebuild", AsyncMock())
    client = _history_client(_run_messages()[:3], responder.settings.mobile_health_bot_id)

    await responder.handle(
        {
            "channel": responder.settings.mobile_health_alert_channel,
            "bot_id": responder.settings.mobile_health_bot_id,
            "ts": "1789630245.000100",
            "text": MOBILE_REPORT,
        },
        client,
    )

    responder.rebuild.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_does_not_rebuild_on_an_earlier_report_of_a_complete_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responder = _responder(tmp_path)
    monkeypatch.setattr("sloperator.alert_dashboard.CHUNK_SETTLE_SECONDS", 0)
    monkeypatch.setattr(responder, "rebuild", AsyncMock())
    client = _history_client(_run_messages(), responder.settings.mobile_health_bot_id)

    await responder.handle(
        {
            "channel": responder.settings.mobile_health_alert_channel,
            "bot_id": responder.settings.mobile_health_bot_id,
            "ts": "1789629900.000100",
            "text": WEB_REPORT,
        },
        client,
    )

    responder.rebuild.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_reports_a_failed_rebuild_to_the_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responder = _responder(tmp_path)
    monkeypatch.setattr("sloperator.alert_dashboard.CHUNK_SETTLE_SECONDS", 0)
    monkeypatch.setattr(
        responder,
        "rebuild",
        AsyncMock(side_effect=RuntimeError("publish exited 1: boom")),
    )
    client = _history_client(_run_messages(), responder.settings.mobile_health_bot_id)
    client.conversations_open.return_value = {"channel": {"id": "DOWNER"}}

    await responder.handle(
        {
            "channel": responder.settings.mobile_health_alert_channel,
            "bot_id": responder.settings.mobile_health_bot_id,
            "ts": "1789630654.000100",
            "text": SUBSCRIPTIONS_REPORT,
        },
        client,
    )

    posted = client.chat_postMessage.await_args.kwargs
    assert posted["channel"] == "DOWNER"
    assert "boom" in posted["text"]
    assert not responder.state_path.exists()
