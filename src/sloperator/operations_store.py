# ruff: noqa: RUF001
"""Durable operational events shared by every runner and the Slack log collector."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
_runtime_database: Path | None = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS operation_events (
 id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', reference TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS operation_events_pending ON operation_events(delivered_at, id);
CREATE TABLE IF NOT EXISTS operation_cursors (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def install_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    # Capture transitions transactionally: a short run cannot disappear between polls,
    # and a crash cannot commit task state without its corresponding log event.
    specs = {
        "scheduled_agent_runs": (
            "NEW.job_name",
            "NEW.run_id",
            "NEW.status",
            "coalesce(NEW.last_error, NEW.result_text, '')",
            "NEW.status IS NOT OLD.status "
            "OR NEW.turn_count IS NOT OLD.turn_count OR NEW.result_text IS NOT OLD.result_text",
        ),
        "agent_requests": (
            "'Slack request ' || NEW.message_ts",
            "NEW.channel_id || '/' || NEW.thread_ts",
            "NEW.status",
            "''",
            "NEW.status IS NOT OLD.status",
        ),
        "agent_sessions": (
            "NEW.provider || ' · ' || NEW.model",
            "NEW.channel_id || '/' || NEW.thread_ts",
            "NEW.status",
            "coalesce(NEW.last_error, '')",
            "NEW.status IS NOT OLD.status OR NEW.turn_count IS NOT OLD.turn_count",
        ),
        "action_runs": (
            "'trigger rule ' || NEW.rule_id",
            "cast(NEW.run_id AS TEXT)",
            "NEW.status",
            "coalesce(NEW.result_json, '')",
            "NEW.status IS NOT OLD.status",
        ),
        "jira_task_agent_links": (
            "'Jira ' || NEW.task_key",
            "NEW.task_key",
            "NEW.phase",
            "coalesce(NEW.confluence_page_url, '')",
            "NEW.phase IS NOT OLD.phase",
        ),
        "delivered_agent_artifacts": (
            "'Slack attachment'",
            "NEW.channel_id || '/' || NEW.thread_ts",
            "'delivered'",
            "'Архив доставлен в исходный тред'",
            "0",
        ),
    }
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, (source, reference, status, detail, changed) in specs.items():
        if table not in tables:
            continue
        for action in ("INSERT", "UPDATE"):
            condition = f"WHEN {changed}" if action == "UPDATE" else ""
            effective_status = status
            if table == "agent_sessions" and action == "INSERT":
                effective_status = "CASE WHEN NEW.status='idle' THEN 'created' ELSE NEW.status END"
            connection.executescript(f"""
CREATE TRIGGER IF NOT EXISTS operations_{table}_{action.lower()}
AFTER {action} ON {table} {condition}
BEGIN
 INSERT INTO operation_events(event_key,source,status,detail,reference)
 VALUES(lower(hex(randomblob(16))),{source},{effective_status},{detail},{reference});
END;
""")


class OperationsStore:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            install_schema(connection)

    def emit(
        self,
        source: str,
        status: str,
        detail: str = "",
        reference: str = "",
        *,
        key: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO operation_events(event_key,source,status,detail,reference) "
                "VALUES(?,?,?,?,?)",
                (key or str(uuid.uuid4()), source, status, detail, reference),
            )

    def cursor(self, name: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM operation_cursors WHERE name=?", (name,)
            ).fetchone()
        return row[0] if row else None

    def checkpoint(self, name: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO operation_cursors VALUES(?,?)", (name, value)
            )

    def pending(self, limit: int = 8) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM operation_events WHERE delivered_at IS NULL ORDER BY id LIMIT ?",
                    (limit,),
                )
            ]

    def delivered(self, ids: list[int]) -> None:
        with self.connect() as connection:
            connection.executemany(
                "UPDATE operation_events SET delivered_at=CURRENT_TIMESTAMP WHERE id=?",
                ((i,) for i in ids),
            )


def configure_operations(path: Path | None) -> None:
    global _runtime_database
    _runtime_database = path


def emit_runtime(source: str, status: str, detail: str = "", reference: str = "") -> None:
    if _runtime_database is not None:
        try:
            OperationsStore(_runtime_database).emit(source, status, detail, reference)
        except (OSError, sqlite3.Error):
            # Observability must never abort the actual job; journald is the fallback.
            LOGGER.exception("Operational event could not be persisted: %s %s", source, status)


def observed(
    source: str,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Observe the common execution boundary, including helper/recovery/admin agents."""

    def decorate(function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(function)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            run_id = str(uuid.uuid4())
            label = source
            for arg in args[:2]:
                if hasattr(arg, "channel_id") and hasattr(arg, "thread_ts"):
                    label += f" · {arg.channel_id}/{arg.thread_ts} · {arg.model}"
            start = time.monotonic()
            emit_runtime(label, "running", reference=run_id)
            try:
                result = await function(*args, **kwargs)
            except asyncio.CancelledError:
                emit_runtime(label, "cancelled", "Выполнение прервано", run_id)
                raise
            except Exception as error:
                emit_runtime(label, "failed", f"{type(error).__name__}: {error}", run_id)
                raise
            else:
                text = getattr(result, "text", result if isinstance(result, str) else "")
                if source == "Admin SQL execution" and isinstance(result, dict):
                    text = f"Запрос выполнен; получено строк: {len(result.get('rows', []))}."
                elif source == "Admin SQL completion":
                    text = "SQL подготовлен; выполнение запроса учитывается отдельно."
                elif source == "Admin visualization":
                    text = "Визуализация подготовлена."
                emit_runtime(label, "returned", f"{time.monotonic() - start:.0f} с. {text}", run_id)
                return result

        return wrapped

    return decorate


def redact(text: str) -> str:
    text = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[скрыто]", text)
    text = re.sub(r"\b(?:xox[baprs]-|sk-ant-|gh[pousr]_)[\w-]+", "[скрыто]", text)
    text = re.sub(
        r"(?i)(password|token|secret|authorization|api[_-]?key)([\s\"'=:\\]+)[^\s,;]+",
        r"\1=[скрыто]",
        text,
    )
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[скрыто]@", text)
    text = re.sub(r"(https?://[^\s?<>]+)\?[^\s<>]+", r"\1?[параметры скрыты]", text)
    return text


def outcome(status: str, detail: str) -> tuple[str, str]:
    lower = detail.lower()
    if status in {"completed", "returned", "idle"}:
        if re.search(
            r"(?:finalisation|finalization|experiment\s+\w+) failed:|(?:^|\n)\s*(?:failed|error):",
            lower,
        ):
            return "❌", "задача не выполнена"
        if re.search(
            r"\b(blocked|frozen|unavailable|quota exceeded|data_unavailable)\b"
            r"|заблокирован|приостановлен",
            lower,
        ):
            return "⛔", "есть блокировка — нужен разбор"
    labels = {
        "created": ("🆕", "сессия создана"),
        "running": ("▶️", "запущен"),
        "queued": ("⏳", "в очереди"),
        "returned": ("↩️", "агент вернул ответ; доставка отдельно"),
        "completed": ("✅", "запуск завершён"),
        "idle": ("✅", "ход завершён"),
        "failed": ("❌", "ошибка"),
        "error": ("❌", "ошибка"),
        "cancelled": ("⏹️", "отменён"),
        "interrupted": ("⏸️", "прерван"),
        "rejected": ("⛔", "отклонён"),
        "delivered": ("📨", "доставлено"),
        "skipped": ("⏭️", "пропущен"),
        "warning": ("⚠️", "предупреждение"),
        "info": ("ℹ️", "событие"),
        "usage": ("📊", "Claude /usage"),
    }
    return labels.get(status, ("ℹ️", status))
