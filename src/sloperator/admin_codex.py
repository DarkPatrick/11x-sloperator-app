"""Admin access to the shared persistent Codex thread store."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sloperator.codex_app_server import CodexAppServer, CodexAppServerError
from sloperator.config import Settings
from sloperator.store import EventStore

LOGGER = logging.getLogger(__name__)

SOURCE_KINDS = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]


def _iso_timestamp(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def _title(thread: Mapping[str, Any]) -> str:
    name = thread.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()[:100]
    preview = thread.get("preview")
    if isinstance(preview, str) and preview.strip():
        return preview.strip().replace("\n", " ")[:100]
    return "Новая сессия"


def _summary(thread: Mapping[str, Any]) -> dict[str, Any]:
    thread_id = str(thread.get("id", ""))
    status = thread.get("status")
    status_type = status.get("type") if isinstance(status, Mapping) else "idle"
    return {
        "session_id": thread_id,
        "external_thread_id": thread_id,
        "title": _title(thread),
        "preview": thread.get("preview") or "",
        "source": thread.get("source") or "unknown",
        "cwd": thread.get("cwd") or "",
        "status": "running" if status_type == "active" else "idle",
        "last_error": None,
        "created_at": _iso_timestamp(thread.get("createdAt")),
        "updated_at": _iso_timestamp(thread.get("updatedAt")),
        "messages": [],
    }


def _text_content(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts = [
        item.get("text", "")
        for item in content
        if isinstance(item, Mapping)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    return "\n".join(part for part in parts if part)


class AdminCodexManager:
    def __init__(self, settings: Settings, store: EventStore) -> None:
        self.settings = settings
        self.store = store  # Kept for compatibility with the existing app wiring.
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._servers: dict[str, CodexAppServer] = {}
        self._cache: list[dict[str, Any]] = []
        self._cache_at = 0.0
        self._cache_lock = asyncio.Lock()
        self._pending_messages: dict[str, list[dict[str, Any]]] = {}
        self._last_errors: dict[str, str] = {}

    def _server(self, *, lock_workspace: bool = False) -> CodexAppServer:
        return CodexAppServer(
            self.settings.codex_cli,
            self.settings.agent_workspace,
            self.settings.codex_model,
            self.settings.agent_timeout_seconds,
            lock_workspace=lock_workspace,
        )

    def _invalidate(self) -> None:
        self._cache_at = 0.0

    async def list_threads(self, *, force: bool = False) -> list[dict[str, Any]]:
        now = asyncio.get_running_loop().time()
        if not force and self._cache and now - self._cache_at < 10:
            return self._with_active_status(self._cache)
        async with self._cache_lock:
            now = asyncio.get_running_loop().time()
            if not force and self._cache and now - self._cache_at < 10:
                return self._with_active_status(self._cache)
            server = self._server()
            try:
                threads = await server.list_threads(SOURCE_KINDS, limit=500)
                self._cache = [_summary(thread) for thread in threads]
                self._cache_at = now
            finally:
                await server.close()
        return self._with_active_status(self._cache)

    def _with_active_status(
        self, sessions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            {**session, "status": "running"}
            if session["session_id"] in self._servers
            else dict(session)
            for session in sessions
        ]

    async def read(self, session_id: str) -> dict[str, Any]:
        server = self._server()
        try:
            thread = await server.read_thread(session_id)
        finally:
            await server.close()
        session = _summary(thread)
        messages: list[dict[str, Any]] = []
        for turn in thread.get("turns", []):
            if not isinstance(turn, Mapping):
                continue
            created_at = _iso_timestamp(turn.get("startedAt"))
            for item in turn.get("items", []):
                if not isinstance(item, Mapping):
                    continue
                role: str | None = None
                text = ""
                if item.get("type") == "userMessage":
                    role = "user"
                    text = _text_content(item.get("content"))
                elif (
                    item.get("type") == "agentMessage"
                    and item.get("phase") == "final_answer"
                    and isinstance(item.get("text"), str)
                ):
                    role = "assistant"
                    text = item["text"]
                if role and text:
                    messages.append(
                        {"role": role, "content": text, "created_at": created_at}
                    )
        session["messages"] = messages
        session["messages"].extend(self._pending_messages.get(session_id, []))
        session["last_error"] = self._last_errors.get(session_id)
        if session_id in self._servers:
            session["status"] = "running"
        elif session["last_error"]:
            session["status"] = "failed"
        return session

    async def create(self, title: str | None = None) -> dict[str, Any]:
        server = self._server()
        try:
            thread_id = await server.start(None)
            clean_title = (title or "").strip()[:100]
            if clean_title:
                await server.set_thread_name(thread_id, clean_title)
        finally:
            await server.close()
        self._invalidate()
        return await self.read(thread_id)

    async def submit(self, session_id: str, text: str) -> str:
        prompt = text.strip()
        if not prompt:
            raise ValueError("Message is empty")
        task = self._tasks.get(session_id)
        server = self._servers.get(session_id)
        if task is not None and not task.done():
            if server is not None and await server.steer(prompt):
                return "steered"
            raise RuntimeError("Session is already running")
        await self.read(session_id)
        self._last_errors.pop(session_id, None)
        self._pending_messages[session_id] = [
            {
                "role": "user",
                "content": prompt,
                "created_at": datetime.now(tz=UTC).isoformat(),
            }
        ]
        self._tasks[session_id] = asyncio.create_task(
            self._run(session_id, prompt), name=f"admin-codex-{session_id}"
        )
        return "started"

    async def _run(self, session_id: str, prompt: str) -> None:
        server = self._server(lock_workspace=True)
        self._servers[session_id] = server
        self._invalidate()
        try:
            await server.start(session_id)
            await server.run_turn(prompt)
        except (TimeoutError, CodexAppServerError, OSError) as error:
            LOGGER.exception("Admin Codex turn failed")
            detail = str(error).strip() or type(error).__name__
            self._last_errors[session_id] = f"Codex turn failed: {detail}"
        else:
            self._pending_messages.pop(session_id, None)
        finally:
            self._servers.pop(session_id, None)
            self._tasks.pop(session_id, None)
            self._invalidate()
            await server.close()

    async def delete(self, session_id: str) -> bool:
        self._pending_messages.pop(session_id, None)
        self._last_errors.pop(session_id, None)
        task = self._tasks.pop(session_id, None)
        server = self._servers.pop(session_id, None)
        if server is not None:
            await server.close()
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        client = self._server()
        try:
            await client.delete_thread(session_id)
        except CodexAppServerError as error:
            if "not found" in str(error).lower():
                return False
            raise
        finally:
            await client.close()
        self._invalidate()
        return True

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._tasks.values()):
            with suppress(asyncio.CancelledError):
                await task
        for server in list(self._servers.values()):
            await server.close()
        self._servers.clear()
        self._tasks.clear()
