"""Private, local-only SQLite event store."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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

CREATE TABLE IF NOT EXISTS agent_sessions (
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    provider TEXT NOT NULL CHECK(provider IN ('claude', 'codex')),
    model TEXT NOT NULL,
    external_session_id TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    turn_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_activity_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, thread_ts)
) STRICT;

CREATE TABLE IF NOT EXISTS agent_requests (
    channel_id TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, message_ts)
) STRICT;

CREATE TABLE IF NOT EXISTS durable_agent_runs (
    channel_id TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    prompt TEXT NOT NULL,
    options_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, message_ts)
) STRICT;

CREATE TABLE IF NOT EXISTS admin_agent_messages (
    message_id INTEGER PRIMARY KEY,
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS scheduled_agent_runs (
    run_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL DEFAULT 'scheduled-agent',
    provider TEXT NOT NULL CHECK(provider IN ('claude', 'codex')),
    model TEXT NOT NULL,
    external_session_id TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    turn_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    prompt TEXT NOT NULL,
    result_text TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS admin_codex_sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    external_thread_id TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS admin_codex_messages (
    message_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES admin_codex_sessions(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS subscription_flow_incidents (
    nature_key TEXT PRIMARY KEY,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    components_json TEXT NOT NULL,
    first_alert_ts TEXT NOT NULL,
    last_alert_ts TEXT NOT NULL,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS anomaly_analysis_cooldowns (
    metric TEXT NOT NULL,
    platform TEXT NOT NULL,
    metric_type TEXT NOT NULL,
    last_launched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (metric, platform, metric_type)
) STRICT;
"""
OTP_MESSAGE_RE = re.compile(r"^\s*(?:vpn\s+otp\s+)?\d{6,8}\s*$", re.IGNORECASE)
REDACTED_OTP = "[redacted one-time code]"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _redact_otp_message(event: Mapping[str, Any]) -> Mapping[str, Any]:
    """Remove ephemeral VPN codes before any local persistence."""
    channel = event.get("channel")
    text = event.get("text")
    if (
        isinstance(channel, str)
        and channel.startswith("D")
        and isinstance(text, str)
        and OTP_MESSAGE_RE.fullmatch(text)
    ):
        redacted = dict(event)
        redacted["text"] = REDACTED_OTP
        return redacted
    return event


@dataclass(frozen=True, slots=True)
class AgentSession:
    """Durable mapping between one Slack thread and one CLI conversation."""

    channel_id: str
    thread_ts: str
    provider: str
    model: str
    external_session_id: str | None
    status: str
    turn_count: int
    last_error: str | None


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
            agent_session_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(agent_sessions)")
            }
            if "last_activity_at" not in agent_session_columns:
                connection.execute(
                    "ALTER TABLE agent_sessions ADD COLUMN last_activity_at TEXT"
                )
                connection.execute(
                    """
                    UPDATE agent_sessions
                    SET last_activity_at = updated_at
                    WHERE last_activity_at IS NULL
                    """
                )
            scheduled_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(scheduled_agent_runs)")
            }
            if "job_name" not in scheduled_columns:
                connection.execute(
                    "ALTER TABLE scheduled_agent_runs ADD COLUMN job_name TEXT "
                    "NOT NULL DEFAULT 'experiment-finalizer'"
                )
                connection.execute(
                    """
                    UPDATE scheduled_agent_runs
                    SET job_name = 'experiment-config-check'
                    WHERE prompt LIKE '%automated review of newly started%'
                       OR prompt LIKE '%configuration audit%'
                       OR prompt LIKE '%configuration with the project%'
                    """
                )
            connection.execute(
                """
                UPDATE admin_codex_sessions
                SET status = 'failed',
                    last_error = 'Service restarted during an active turn',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', '5')"
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
        event = _redact_otp_message(event)
        if body.get("event") is not event:
            body = {**body, "event": event}
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
                self._upsert_message(
                    connection,
                    _redact_otp_message({**message, "channel": channel_id}),
                    channel_id,
                )

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
                "agent_sessions": "SELECT count(*) FROM agent_sessions",
                "agent_requests": "SELECT count(*) FROM agent_requests",
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

    def contains_channel(self, channel_id: str) -> bool:
        """Return whether a conversation is already present in the map."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM channels WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
        return row is not None

    def get_agent_session(self, channel_id: str, thread_ts: str) -> AgentSession | None:
        """Load the agent session associated with a Slack thread."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT channel_id, thread_ts, provider, model, external_session_id,
                       status, turn_count, last_error
                FROM agent_sessions
                WHERE channel_id = ? AND thread_ts = ?
                """,
                (channel_id, thread_ts),
            ).fetchone()
        return AgentSession(*row) if row is not None else None

    def list_agent_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent agent sessions with channel names for the admin UI."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT s.channel_id, COALESCE(c.name, s.channel_id) AS channel_name,
                       s.thread_ts, s.provider, s.model, s.external_session_id,
                       s.status, s.turn_count, s.last_error, s.created_at,
                       s.updated_at, s.last_activity_at
                FROM agent_sessions AS s
                LEFT JOIN channels AS c ON c.channel_id = s.channel_id
                ORDER BY datetime(s.updated_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_slack_trigger_runs(self, days: int = 28) -> list[dict[str, Any]]:
        """Return recent event-driven agent launches for the admin trigger calendar."""
        suffixes = {
            ":monetisation-analysis": "analytics-anomaly",
            ":subscription-flow-analysis": "subscription-flow",
            ":mobile-health-analysis": "mobile-health",
        }
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT r.channel_id, COALESCE(c.name, r.channel_id) AS channel_name,
                       r.message_ts, r.thread_ts, r.status, r.created_at, r.updated_at,
                       s.status AS session_status, s.provider, s.model,
                       s.external_session_id
                FROM agent_requests AS r
                LEFT JOIN agent_sessions AS s
                  ON s.channel_id = r.channel_id AND s.thread_ts = r.thread_ts
                LEFT JOIN channels AS c ON c.channel_id = r.channel_id
                WHERE datetime(r.created_at) >= datetime('now', ?)
                  AND (
                    r.message_ts LIKE '%:monetisation-analysis'
                    OR r.message_ts LIKE '%:subscription-flow-analysis'
                    OR r.message_ts LIKE '%:mobile-health-analysis'
                  )
                ORDER BY datetime(r.created_at) DESC
                """,
                (f"-{days} days",),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            message_ts = item["message_ts"]
            trigger = next(
                key for suffix, key in suffixes.items() if message_ts.endswith(suffix)
            )
            item["trigger"] = trigger
            item["session_exists"] = item["session_status"] is not None
            item["slack_url"] = (
                f"https://slack.com/archives/{item['channel_id']}/"
                f"p{item['thread_ts'].replace('.', '')}"
            )
            result.append(item)
        return result

    def thread_messages(
        self, channel_id: str, thread_ts: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Return recent persisted messages from one Slack thread."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT message_ts, user_id, bot_id, text, updated_at
                FROM (
                    SELECT message_ts, user_id, bot_id, text, updated_at,
                           CAST(message_ts AS REAL) AS sort_key
                    FROM messages
                    WHERE channel_id = ?
                      AND (message_ts = ? OR thread_ts = ?)
                      AND deleted = 0
                    UNION ALL
                    SELECT 'admin:' || message_id, 'admin', NULL, text, created_at,
                           CAST(strftime('%s', created_at) AS REAL)
                    FROM admin_agent_messages
                    WHERE channel_id = ? AND thread_ts = ?
                )
                ORDER BY sort_key DESC
                LIMIT ?
                """,
                (channel_id, thread_ts, thread_ts, channel_id, thread_ts, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_admin_agent_message(
        self, channel_id: str, thread_ts: str, text: str
    ) -> None:
        """Persist a message submitted through the local admin UI."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_agent_messages(channel_id, thread_ts, text)
                VALUES (?, ?, ?)
                """,
                (channel_id, thread_ts, text),
            )

    def create_scheduled_agent_run(
        self,
        run_id: str,
        job_name: str,
        provider: str,
        model: str,
        external_session_id: str | None,
        prompt: str,
    ) -> None:
        """Persist a headless cron run before the provider starts."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduled_agent_runs(
                    run_id, job_name, provider, model, external_session_id, prompt
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, job_name, provider, model, external_session_id, prompt),
            )

    def finish_scheduled_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        external_session_id: str | None = None,
        result_text: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """Record the terminal state and final provider response of a cron run."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE scheduled_agent_runs
                SET status = ?,
                    external_session_id = COALESCE(?, external_session_id),
                    turn_count = CASE WHEN ? = 'completed' THEN 1 ELSE turn_count END,
                    result_text = COALESCE(?, result_text),
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = ?
                """,
                (status, external_session_id, status, result_text, last_error, run_id),
            )

    def list_scheduled_agent_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return durable cron/headless runs in the agent-session UI shape."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT run_id, job_name, provider, model, external_session_id, status,
                       turn_count, last_error, prompt, result_text,
                       created_at, updated_at
                FROM scheduled_agent_runs
                ORDER BY datetime(updated_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            run_id = item.pop("run_id")
            job_name = item.pop("job_name")
            messages = [
                {
                    "message_ts": "prompt",
                    "user_id": "scheduler",
                    "bot_id": None,
                    "text": item.pop("prompt"),
                    "updated_at": item["created_at"],
                }
            ]
            result_text = item.pop("result_text")
            if result_text:
                messages.append(
                    {
                        "message_ts": "result",
                        "user_id": "agent",
                        "bot_id": None,
                        "text": result_text,
                        "updated_at": item["updated_at"],
                    }
                )
            result.append(
                {
                    **item,
                    "channel_id": "scheduled",
                    "channel_name": job_name,
                    "thread_ts": run_id,
                    "last_activity_at": item["updated_at"],
                    "headless": True,
                    "messages": messages,
                }
            )
        return result

    def delete_scheduled_agent_run(self, run_id: str) -> bool:
        """Permanently remove one completed scheduled run from admin history."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM scheduled_agent_runs WHERE run_id = ?",
                (run_id,),
            )
        return cursor.rowcount == 1

    def create_admin_codex_session(self, session_id: str, title: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO admin_codex_sessions(session_id, title) VALUES (?, ?)",
                (session_id, title),
            )

    def list_admin_codex_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            sessions = connection.execute(
                """
                SELECT session_id, title, external_thread_id, status, last_error,
                       created_at, updated_at
                FROM admin_codex_sessions
                ORDER BY datetime(updated_at) DESC
                """
            ).fetchall()
            result: list[dict[str, Any]] = []
            for session in sessions:
                item = dict(session)
                messages = connection.execute(
                    """
                    SELECT message_id, role, text, created_at
                    FROM admin_codex_messages
                    WHERE session_id = ?
                    ORDER BY message_id
                    """,
                    (item["session_id"],),
                ).fetchall()
                item["messages"] = [dict(message) for message in messages]
                result.append(item)
        return result

    def get_admin_codex_session(self, session_id: str) -> dict[str, Any] | None:
        return next(
            (
                session
                for session in self.list_admin_codex_sessions()
                if session["session_id"] == session_id
            ),
            None,
        )

    def add_admin_codex_message(self, session_id: str, role: str, text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_codex_messages(session_id, role, text)
                VALUES (?, ?, ?)
                """,
                (session_id, role, text),
            )
            connection.execute(
                """
                UPDATE admin_codex_sessions
                SET updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
                """,
                (session_id,),
            )

    def update_admin_codex_session(
        self,
        session_id: str,
        *,
        status: str,
        external_thread_id: str | None = None,
        last_error: str | None = None,
        title: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE admin_codex_sessions
                SET status = ?,
                    external_thread_id = COALESCE(?, external_thread_id),
                    last_error = ?,
                    title = COALESCE(?, title),
                    updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
                """,
                (status, external_thread_id, last_error, title, session_id),
            )

    def delete_admin_codex_session(self, session_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM admin_codex_sessions WHERE session_id = ?",
                (session_id,),
            )
        return cursor.rowcount == 1

    def close_agent_session(self, channel_id: str, thread_ts: str) -> bool:
        """Permanently close a session while retaining its audit history."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_sessions
                SET status = 'closed', last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND thread_ts = ?
                  AND status != 'closed'
                """,
                (channel_id, thread_ts),
            )
            connection.execute(
                """
                UPDATE agent_requests
                SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND thread_ts = ? AND status = 'queued'
                """,
                (channel_id, thread_ts),
            )
        return cursor.rowcount == 1

    def has_agent_thread(self, channel_id: str, thread_ts: str) -> bool:
        """Return whether a thread has a session or a queued/running agent request."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM agent_sessions
                WHERE channel_id = ? AND thread_ts = ?
                UNION ALL
                SELECT 1
                FROM agent_requests
                WHERE channel_id = ? AND thread_ts = ?
                LIMIT 1
                """,
                (channel_id, thread_ts, channel_id, thread_ts),
            ).fetchone()
        return row is not None

    def claim_subscription_flow_incident(
        self,
        nature_key: str,
        components: set[str],
        alert_ts: str,
    ) -> bool:
        """Open or extend an incident; return true only when Claude should be launched."""
        if not nature_key or not components:
            raise ValueError("Subscription-flow incidents need a nature and components")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT active, components_json
                FROM subscription_flow_incidents
                WHERE nature_key = ?
                """,
                (nature_key,),
            ).fetchone()
            if row is None:
                component_alerts = {component: alert_ts for component in sorted(components)}
                connection.execute(
                    """
                    INSERT INTO subscription_flow_incidents(
                        nature_key, active, components_json, first_alert_ts, last_alert_ts
                    ) VALUES (?, 1, ?, ?, ?)
                    """,
                    (nature_key, _json(component_alerts), alert_ts, alert_ts),
                )
                return True
            active, raw_components = bool(row[0]), row[1]
            decoded = json.loads(raw_components)
            existing = (
                decoded
                if isinstance(decoded, dict)
                else {component: alert_ts for component in decoded}
            )
            merged = {**existing, **dict.fromkeys(sorted(components), alert_ts)}
            if active:
                connection.execute(
                    """
                    UPDATE subscription_flow_incidents
                    SET components_json = ?, last_alert_ts = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE nature_key = ?
                    """,
                    (_json(merged), alert_ts, nature_key),
                )
                return False
            connection.execute(
                """
                UPDATE subscription_flow_incidents
                SET active = 1, components_json = ?, first_alert_ts = ?,
                    last_alert_ts = ?, resolved_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE nature_key = ?
                """,
                (
                    _json({component: alert_ts for component in sorted(components)}),
                    alert_ts,
                    alert_ts,
                    nature_key,
                ),
            )
        return True

    def claim_anomaly_analyses(
        self,
        keys: Sequence[tuple[str, str, str]],
        cooldown_hours: int = 24,
    ) -> set[tuple[str, str, str]]:
        """Reserve only anomaly dimensions not analysed within the cooldown."""
        claimed: set[tuple[str, str, str]] = set()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for key in dict.fromkeys(keys):
                row = connection.execute(
                    """
                    SELECT datetime(last_launched_at) > datetime('now', ?)
                    FROM anomaly_analysis_cooldowns
                    WHERE metric = ? AND platform = ? AND metric_type = ?
                    """,
                    (f"-{cooldown_hours} hours", *key),
                ).fetchone()
                if row is not None and bool(row[0]):
                    continue
                connection.execute(
                    """
                    INSERT INTO anomaly_analysis_cooldowns(
                        metric, platform, metric_type, last_launched_at
                    ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(metric, platform, metric_type) DO UPDATE SET
                        last_launched_at = CURRENT_TIMESTAMP
                    """,
                    key,
                )
                claimed.add(key)
        return claimed

    def close_subscription_flow_component(
        self,
        component: str,
        closure_alert_ts: str,
    ) -> int:
        """Apply a terminal component reply and resolve incidents with no affected flows left."""
        resolved = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT nature_key, components_json
                FROM subscription_flow_incidents
                WHERE active = 1
                """
            ).fetchall()
            for nature_key, raw_components in rows:
                decoded = json.loads(raw_components)
                components = (
                    decoded
                    if isinstance(decoded, dict)
                    else {item: closure_alert_ts for item in decoded}
                )
                if component not in components:
                    continue
                if float(closure_alert_ts) < float(components[component]):
                    continue
                components.pop(component)
                if components:
                    connection.execute(
                        """
                        UPDATE subscription_flow_incidents
                        SET components_json = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE nature_key = ?
                        """,
                        (_json(components), nature_key),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE subscription_flow_incidents
                        SET active = 0, components_json = '{}',
                            resolved_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE nature_key = ?
                        """,
                        (nature_key,),
                    )
                    resolved += 1
        return resolved

    def create_agent_session(
        self,
        channel_id: str,
        thread_ts: str,
        provider: str,
        model: str,
        external_session_id: str | None = None,
    ) -> AgentSession:
        """Create a durable agent session before its first CLI turn."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_sessions(
                    channel_id, thread_ts, provider, model, external_session_id,
                    last_activity_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (channel_id, thread_ts, provider, model, external_session_id),
            )
        session = self.get_agent_session(channel_id, thread_ts)
        if session is None:
            raise RuntimeError("Agent session was not persisted")
        return session

    def start_agent_turn(self, channel_id: str, thread_ts: str) -> None:
        """Mark a session busy before starting a CLI process."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_sessions
                SET status = 'running', last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND thread_ts = ?
                """,
                (channel_id, thread_ts),
            )

    def set_agent_external_session_id(
        self,
        channel_id: str,
        thread_ts: str,
        external_session_id: str,
    ) -> None:
        """Persist a provider session ID as soon as the provider allocates it."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_sessions
                SET external_session_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND thread_ts = ?
                """,
                (external_session_id, channel_id, thread_ts),
            )

    def finish_agent_turn(
        self,
        channel_id: str,
        thread_ts: str,
        external_session_id: str,
    ) -> None:
        """Persist a successful CLI turn and its resumable session ID."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_sessions
                SET external_session_id = ?, status = 'idle',
                    turn_count = turn_count + 1, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND thread_ts = ?
                """,
                (external_session_id, channel_id, thread_ts),
            )

    def fail_agent_turn(
        self,
        channel_id: str,
        thread_ts: str,
        error: str,
    ) -> None:
        """Persist a failed turn without discarding resumable session state."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_sessions
                SET status = 'failed', last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND thread_ts = ?
                """,
                (error[:4_000], channel_id, thread_ts),
            )

    def cancel_agent_turn(self, channel_id: str, thread_ts: str) -> None:
        """Record a user-requested cancellation without losing session identity."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_sessions
                SET status = 'cancelled', last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND thread_ts = ?
                """,
                (channel_id, thread_ts),
            )

    def recover_interrupted_agent_work(self) -> int:
        """Mark nonterminal work left by a previous process as cancelled."""
        with self._connect() as connection:
            sessions = connection.execute(
                """
                UPDATE agent_sessions
                SET status = 'cancelled', last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                """
            ).rowcount
            connection.execute(
                """
                UPDATE agent_requests
                SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE status = 'queued'
                """
            )
        return sessions

    def claim_agent_request(self, channel_id: str, message_ts: str, thread_ts: str) -> bool:
        """Deduplicate Slack retries before they can launch a paid agent turn."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO agent_requests(channel_id, message_ts, thread_ts)
                VALUES (?, ?, ?)
                """,
                (channel_id, message_ts, thread_ts),
            )
        return cursor.rowcount == 1

    def prepare_agent_request(
        self,
        channel_id: str,
        message_ts: str,
        thread_ts: str,
        inactivity_hours: int = 24,
    ) -> str:
        """Claim a request and permanently expire an inactive thread session."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                """
                SELECT 1 FROM agent_requests
                WHERE channel_id = ? AND message_ts = ?
                """,
                (channel_id, message_ts),
            ).fetchone():
                return "duplicate"
            session = connection.execute(
                """
                SELECT status,
                       COALESCE(last_activity_at, updated_at)
                FROM agent_sessions
                WHERE channel_id = ? AND thread_ts = ?
                """,
                (channel_id, thread_ts),
            ).fetchone()
            if session is not None:
                status, last_activity_at = session
                terminal = status in {"expired", "closed"}
                expired = terminal or connection.execute(
                    """
                    SELECT datetime(?) <= datetime('now', ?)
                    """,
                    (last_activity_at, f"-{inactivity_hours} hours"),
                ).fetchone()[0]
                if expired:
                    if not terminal:
                        connection.execute(
                            """
                            UPDATE agent_sessions
                            SET status = 'expired', updated_at = CURRENT_TIMESTAMP
                            WHERE channel_id = ? AND thread_ts = ?
                            """,
                            (channel_id, thread_ts),
                        )
                    connection.execute(
                        """
                        INSERT INTO agent_requests(
                            channel_id, message_ts, thread_ts, status
                        ) VALUES (?, ?, ?, 'expired')
                        """,
                        (channel_id, message_ts, thread_ts),
                    )
                    return "expired"
                connection.execute(
                    """
                    UPDATE agent_sessions
                    SET last_activity_at = CURRENT_TIMESTAMP
                    WHERE channel_id = ? AND thread_ts = ?
                    """,
                    (channel_id, thread_ts),
                )
            connection.execute(
                """
                INSERT INTO agent_requests(channel_id, message_ts, thread_ts)
                VALUES (?, ?, ?)
                """,
                (channel_id, message_ts, thread_ts),
            )
        return "claimed"

    def finish_agent_request(self, channel_id: str, message_ts: str, status: str) -> None:
        """Record the terminal state of a Slack agent request."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_requests
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND message_ts = ?
                """,
                (status, channel_id, message_ts),
            )

    def save_durable_agent_run(
        self,
        channel_id: str,
        message_ts: str,
        thread_ts: str,
        prompt: str,
        options: Mapping[str, object],
    ) -> None:
        """Persist an automated turn so a service restart can resume it."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO durable_agent_runs(
                    channel_id, message_ts, thread_ts, prompt, options_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, message_ts) DO UPDATE SET
                    thread_ts = excluded.thread_ts,
                    prompt = excluded.prompt,
                    options_json = excluded.options_json,
                    status = 'queued',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (channel_id, message_ts, thread_ts, prompt, json.dumps(options)),
            )

    def set_durable_agent_run_status(
        self, channel_id: str, message_ts: str, status: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE durable_agent_runs
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND message_ts = ?
                """,
                (status, channel_id, message_ts),
            )

    def find_recent_completed_analysis(
        self, channel_id: str, reuse_key: str, days: int = 5
    ) -> str | None:
        """Return the newest completed automated thread with the same identity set."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT thread_ts
                FROM durable_agent_runs
                WHERE channel_id = ?
                  AND status = 'completed'
                  AND json_extract(options_json, '$.reuse_key') = ?
                  AND datetime(updated_at) >= datetime('now', ?)
                ORDER BY datetime(updated_at) DESC
                LIMIT 1
                """,
                (channel_id, reuse_key, f"-{days} days"),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def list_interrupted_durable_agent_runs(self) -> list[dict[str, object]]:
        """Return durable turns that did not reach a terminal result."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT channel_id, message_ts, thread_ts, prompt, options_json
                FROM durable_agent_runs
                WHERE status IN ('queued', 'running', 'interrupted')
                ORDER BY created_at
                """
            ).fetchall()
        return [
            {
                "channel_id": row[0],
                "message_ts": row[1],
                "thread_ts": row[2],
                "prompt": row[3],
                "options": json.loads(row[4]),
            }
            for row in rows
        ]

    def list_interrupted_scheduled_agent_runs(
        self, job_name: str | None = None
    ) -> list[dict[str, object]]:
        """Return headless cron turns interrupted before producing a result."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, job_name, provider, model, external_session_id, prompt,
                       status, result_text
                FROM scheduled_agent_runs
                WHERE status IN ('running', 'interrupted', 'recovered')
                  AND (? IS NULL OR job_name = ?)
                ORDER BY created_at
                """,
                (job_name, job_name),
            ).fetchall()
        return [
            {
                "run_id": row[0],
                "job_name": row[1],
                "provider": row[2],
                "model": row[3],
                "external_session_id": row[4],
                "prompt": row[5],
                "status": row[6],
                "result_text": row[7],
            }
            for row in rows
        ]

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
