from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

from sloperator.admin_codex import AdminCodexManager
from sloperator.config import Settings
from sloperator.store import EventStore


class FakeCodexServer:
    threads: ClassVar[dict[str, dict[str, Any]]] = {}
    counter = 0

    def __init__(self, *_args, **_kwargs) -> None:
        self.thread_id: str | None = None
        self.closed = False

    async def start(self, existing_thread_id: str | None) -> str:
        if existing_thread_id is None:
            type(self).counter += 1
            existing_thread_id = f"thread-{self.counter}"
            self.threads[existing_thread_id] = {
                "id": existing_thread_id,
                "name": None,
                "preview": "",
                "source": "appServer",
                "status": {"type": "notLoaded"},
                "createdAt": 100,
                "updatedAt": 100,
                "cwd": "/workspace",
                "turns": [],
            }
        self.thread_id = existing_thread_id
        return existing_thread_id

    async def list_threads(self, _sources, limit=100):
        return list(self.threads.values())[:limit]

    async def read_thread(self, thread_id: str):
        return self.threads[thread_id]

    async def set_thread_name(self, thread_id: str, name: str) -> None:
        self.threads[thread_id]["name"] = name

    async def run_turn(self, prompt: str) -> str:
        assert self.thread_id
        self.threads[self.thread_id]["preview"] = prompt
        self.threads[self.thread_id]["updatedAt"] = 101
        self.threads[self.thread_id]["turns"].append(
            {
                "startedAt": 101,
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": prompt}],
                    },
                    {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "working",
                    },
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": f"answer: {prompt}",
                    },
                ],
            }
        )
        return f"answer: {prompt}"

    async def steer(self, _text: str) -> bool:
        return True

    async def delete_thread(self, thread_id: str) -> None:
        del self.threads[thread_id]

    async def close(self) -> None:
        self.closed = True


async def test_admin_codex_manager_uses_shared_threads(tmp_path: Path) -> None:
    FakeCodexServer.threads = {}
    FakeCodexServer.counter = 0
    store = EventStore(tmp_path / "archive.sqlite3")
    store.initialize()
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        agent_workspace=tmp_path,
    )
    manager = AdminCodexManager(settings, store)

    with patch("sloperator.admin_codex.CodexAppServer", FakeCodexServer):
        session = await manager.create("Shared session")
        assert session["session_id"] == "thread-1"
        assert session["title"] == "Shared session"
        assert (await manager.list_threads())[0]["source"] == "appServer"

        assert await manager.submit(session["session_id"], "Inspect this") == "started"
        task = manager._tasks[session["session_id"]]
        running = await manager.read(session["session_id"])
        assert running["messages"][-1]["content"] == "Inspect this"
        await asyncio.shield(task)
        persisted = await manager.read(session["session_id"])

        assert [(item["role"], item["content"]) for item in persisted["messages"]] == [
            ("user", "Inspect this"),
            ("assistant", "answer: Inspect this"),
        ]
        assert await manager.delete(session["session_id"])
        assert await manager.list_threads(force=True) == []
