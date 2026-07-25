from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

from sloperator.store import EventStore


def test_store_is_private_and_deduplicates_events(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "private" / "archive.sqlite3")
    store.initialize()
    body = {
        "event_id": "Ev123",
        "event": {
            "type": "message",
            "channel": "C123",
            "user": "U123",
            "ts": "123.456",
            "text": "private message",
        },
    }

    store.record_event(body, body["event"])
    store.record_event(body, body["event"])

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.summary()["events"] == 1
    assert store.summary()["messages"] == 1


def test_store_tracks_channels_threads_and_deletions(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "archive.sqlite3")
    store.initialize()
    store.upsert_channels(
        [{"id": "C123", "name": "ops", "is_member": True, "is_private": False}],
        "channel",
    )
    store.upsert_history_messages(
        "C123",
        [
            {"ts": "1.0", "user": "U1", "text": "root"},
            {"ts": "2.0", "thread_ts": "1.0", "user": "U2", "text": "reply"},
        ],
    )
    deleted = {
        "event_id": "EvDeleted",
        "event": {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "C123",
            "deleted_ts": "2.0",
            "previous_message": {
                "ts": "2.0",
                "thread_ts": "1.0",
                "user": "U2",
                "text": "reply",
            },
        },
    }

    store.record_event(deleted, deleted["event"])

    summary = store.summary()
    assert summary["channels"] == 1
    assert summary["member_channels"] == 1
    assert summary["threads"] == 1
    assert store.contains_channel("C123")
    assert not store.contains_channel("C999")


def test_store_tracks_agent_sessions_and_deduplicates_requests(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "archive.sqlite3")
    store.initialize()

    assert store.claim_agent_request("D123", "2.0", "1.0")
    assert not store.claim_agent_request("D123", "2.0", "1.0")

    created = store.create_agent_session(
        "D123",
        "1.0",
        "claude",
        "opus",
        "00000000-0000-0000-0000-000000000001",
    )
    assert created.turn_count == 0
    assert created.provider == "claude"

    store.start_agent_turn("D123", "1.0")
    store.finish_agent_turn(
        "D123",
        "1.0",
        "00000000-0000-0000-0000-000000000001",
    )
    finished = store.get_agent_session("D123", "1.0")

    assert finished is not None
    assert finished.status == "idle"
    assert finished.turn_count == 1

    store.start_agent_turn("D123", "1.0")
    store.cancel_agent_turn("D123", "1.0")
    cancelled = store.get_agent_session("D123", "1.0")

    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.external_session_id == finished.external_session_id
    assert cancelled.turn_count == 1

    store.start_agent_turn("D123", "1.0")
    assert store.claim_agent_request("D123", "3.0", "1.0")

    assert store.recover_interrupted_agent_work() == 1
    recovered = store.get_agent_session("D123", "1.0")

    assert recovered is not None
    assert recovered.status == "cancelled"


def test_store_redacts_vpn_otp_from_event_and_message(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "archive.sqlite3")
    store.initialize()
    body = {
        "event_id": "EvOtp",
        "event": {
            "type": "message",
            "channel": "D123",
            "user": "U123",
            "ts": "123.456",
            "text": "vpn otp 123456",
        },
    }

    store.record_event(body, body["event"])

    with sqlite3.connect(store.path) as connection:
        payload = connection.execute(
            "SELECT payload_json FROM events WHERE event_id = 'EvOtp'"
        ).fetchone()[0]
        text, raw = connection.execute(
            "SELECT text, raw_json FROM messages WHERE message_ts = '123.456'"
        ).fetchone()
    assert "123456" not in payload
    assert "123456" not in text
    assert "123456" not in raw
