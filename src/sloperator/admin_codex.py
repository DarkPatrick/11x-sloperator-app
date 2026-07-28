"""Local admin chat sessions backed by persistent Codex threads."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from typing import Any

from sloperator.codex_app_server import CodexAppServer, CodexAppServerError
from sloperator.config import Settings
from sloperator.store import EventStore

LOGGER = logging.getLogger(__name__)


class AdminCodexManager:
    def __init__(self, settings: Settings, store: EventStore) -> None:
        self.settings = settings
        self.store = store
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._servers: dict[str, CodexAppServer] = {}

    async def create(self, title: str | None = None) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        await asyncio.to_thread(
            self.store.create_admin_codex_session,
            session_id,
            (title or "Новая сессия").strip()[:100] or "Новая сессия",
        )
        session = await asyncio.to_thread(self.store.get_admin_codex_session, session_id)
        if session is None:
            raise RuntimeError("Codex session was not persisted")
        return session

    async def submit(self, session_id: str, text: str) -> str:
        prompt = text.strip()
        if not prompt:
            raise ValueError("Message is empty")
        session = await asyncio.to_thread(self.store.get_admin_codex_session, session_id)
        if session is None:
            raise KeyError(session_id)
        server = self._servers.get(session_id)
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            if server is not None and await server.steer(prompt):
                await asyncio.to_thread(
                    self.store.add_admin_codex_message, session_id, "user", prompt
                )
                return "steered"
            raise RuntimeError("Session is already running")
        await asyncio.to_thread(
            self.store.add_admin_codex_message, session_id, "user", prompt
        )
        task = asyncio.create_task(
            self._run(session_id, prompt), name=f"admin-codex-{session_id}"
        )
        self._tasks[session_id] = task
        return "started"

    async def _run(self, session_id: str, prompt: str) -> None:
        session = await asyncio.to_thread(self.store.get_admin_codex_session, session_id)
        if session is None:
            return
        title = session["title"]
        if title == "Новая сессия":
            title = prompt.replace("\n", " ")[:60]
        await asyncio.to_thread(
            self.store.update_admin_codex_session,
            session_id,
            status="running",
            title=title,
        )
        server = CodexAppServer(
            self.settings.codex_cli,
            self.settings.agent_workspace,
            self.settings.codex_model,
            self.settings.agent_timeout_seconds,
        )
        self._servers[session_id] = server
        try:
            thread_id = await server.start(session["external_thread_id"])
            await asyncio.to_thread(
                self.store.update_admin_codex_session,
                session_id,
                status="running",
                external_thread_id=thread_id,
            )
            response = await server.run_turn(prompt)
            await asyncio.to_thread(
                self.store.add_admin_codex_message,
                session_id,
                "assistant",
                response,
            )
            await asyncio.to_thread(
                self.store.update_admin_codex_session,
                session_id,
                status="idle",
            )
        except (TimeoutError, CodexAppServerError, OSError) as error:
            LOGGER.exception("Admin Codex turn failed")
            await asyncio.to_thread(
                self.store.update_admin_codex_session,
                session_id,
                status="failed",
                last_error=f"{type(error).__name__}: {error}",
            )
        finally:
            self._servers.pop(session_id, None)
            await server.close()

    async def delete(self, session_id: str) -> bool:
        task = self._tasks.pop(session_id, None)
        server = self._servers.pop(session_id, None)
        if server is not None:
            await server.close()
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        return await asyncio.to_thread(self.store.delete_admin_codex_session, session_id)

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._tasks.values()):
            with suppress(asyncio.CancelledError):
                await task
        self._servers.clear()
        self._tasks.clear()
