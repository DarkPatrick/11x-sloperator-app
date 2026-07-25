"""Private, local-only SQLite event store."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS workspaces (
    team_id TEXT PRIMARY KEY,
    name TEXT,
    bot_user_id TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    name TEXT,
    kind TEXT NOT NULL,
    is_private INTEGER NOT NULL DEFAULT 0,
    is_member INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    event_ts TEXT,
    channel_id TEXT,
    user_id TEXT,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS messages (
    channel_id TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    thread_ts TEXT,
    user_id TEXT,
    bot_id TEXT,
    subtype TEXT,
    text TEXT NOT NULL DEFAULT '',
    deleted INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, message_ts)
) STRICT;

CREATE INDEX IF NOT EXISTS messages_thread_idx
ON messages(channel_id, thread_ts, message_ts);

CREATE INDEX IF NOT EXISTS events_channel_idx
ON events(channel_id, event_ts);

CREATE TABLE IF NOT EXISTS trigger_rules (
    rule_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 0,
    condition_json TEXT NOT NULL,
    action_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS action_runs (
    run_id INTEGER PRIMARY KEY,
    rule_id INTEGER NOT NULL REFERENCES trigger_rules(rule_id),
    source_event_id TEXT REFERENCES events(event_id),
    status TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;
"""


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class EventStore:
    """SQLite repository with one short-lived connection per operation."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def initialize(self) -> None:
        """Create a private database and apply idempotent schema migrations."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', '1')"
            )
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def upsert_workspace(self, team_id: str, name: str, bot_user_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workspaces(team_id, name, bot_user_id)
                VALUES (?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    name=excluded.name,
                    bot_user_id=excluded.bot_user_id,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (team_id, name, bot_user_id),
            )

    def upsert_channels(self, channels: Sequence[Mapping[str, Any]], kind: str) -> None:
        rows = [
            (
                channel["id"],
                channel.get("name") or channel.get("user"),
                kind,
                int(bool(channel.get("is_private"))),
                int(bool(channel.get("is_member"))),
                int(bool(channel.get("is_archived"))),
                _json(channel),
            )
            for channel in channels
            if isinstance(channel.get("id"), str)
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO channels(
                    channel_id, name, kind, is_private, is_member, is_archived, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    name=excluded.name,
                    kind=excluded.kind,
                    is_private=excluded.is_private,
                    is_member=excluded.is_member,
                    is_archived=excluded.is_archived,
                    raw_json=excluded.raw_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                rows,
            )

    def record_event(self, body: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        """Persist an Events API envelope and its normalized message."""
        event_id = body.get("event_id")
        if not isinstance(event_id, str):
            event_id = (
                f"{event.get('type', 'unknown')}:{event.get('event_ts', event.get('ts', ''))}"
            )
        channel_id = event.get("channel")
        user_id = event.get("user")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_id, event_type, event_ts, channel_id, user_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    str(event.get("type", "unknown")),
                    event.get("event_ts") or event.get("ts"),
                    channel_id if isinstance(channel_id, str) else None,
                    user_id if isinstance(user_id, str) else None,
                    _json(body),
                ),
            )
            if event.get("type") == "message":
                self._upsert_message(connection, event)

    def upsert_history_messages(
        self, channel_id: str, messages: Sequence[Mapping[str, Any]]
    ) -> None:
        with self._connect() as connection:
            for message in messages:
                self._upsert_message(connection, message, channel_id)

    def _upsert_message(
        self,
        connection: sqlite3.Connection,
        event: Mapping[str, Any],
        fallback_channel_id: str | None = None,
    ) -> None:
        channel_id = event.get("channel") or fallback_channel_id
        message: Mapping[str, Any] = event
        deleted = False

        if event.get("subtype") == "message_changed" and isinstance(event.get("message"), Mapping):
            message = event["message"]
        elif event.get("subtype") == "message_deleted":
            previous = event.get("previous_message")
            message = previous if isinstance(previous, Mapping) else event
            deleted = True

        message_ts = message.get("ts") or event.get("deleted_ts")
        if not isinstance(channel_id, str) or not isinstance(message_ts, str):
            return

        connection.execute(
            """
            INSERT INTO messages(
                channel_id, message_ts, thread_ts, user_id, bot_id, subtype,
                text, deleted, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, message_ts) DO UPDATE SET
                thread_ts=excluded.thread_ts,
                user_id=excluded.user_id,
                bot_id=excluded.bot_id,
                subtype=excluded.subtype,
                text=excluded.text,
                deleted=excluded.deleted,
                raw_json=excluded.raw_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                channel_id,
                message_ts,
                message.get("thread_ts"),
                message.get("user"),
                message.get("bot_id"),
                message.get("subtype"),
                str(message.get("text", "")),
                int(deleted),
                _json(message),
            ),
        )

    def summary(self) -> dict[str, int]:
        """Return non-sensitive archive counts."""
        with self._connect() as connection:
            keys = {
                "channels": "SELECT count(*) FROM channels",
                "member_channels": "SELECT count(*) FROM channels WHERE is_member = 1",
                "events": "SELECT count(*) FROM events",
                "messages": "SELECT count(*) FROM messages",
                "threads": (
                    "SELECT count(DISTINCT channel_id || ':' || thread_ts) "
                    "FROM messages WHERE thread_ts IS NOT NULL"
                ),
                "enabled_rules": "SELECT count(*) FROM trigger_rules WHERE enabled = 1",
            }
            return {
                key: int(connection.execute(query).fetchone()[0]) for key, query in keys.items()
            }

    def contains_message(self, channel_id: str, message_ts: str) -> bool:
        """Return whether a message is already present in the archive."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM messages
                WHERE channel_id = ? AND message_ts = ?
                """,
                (channel_id, message_ts),
            ).fetchone()
        return row is not None

    def channel_map(self) -> list[tuple[str, str | None, str, bool]]:
        """Return channel identifiers and membership without message content."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT channel_id, name, kind, is_member
                FROM channels
                ORDER BY is_member DESC, kind, name
                """
            ).fetchall()
        return [(row[0], row[1], row[2], bool(row[3])) for row in rows]
