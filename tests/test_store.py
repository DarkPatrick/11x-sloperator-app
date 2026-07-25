from __future__ import annotations

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
