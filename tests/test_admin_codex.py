from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sloperator.admin_codex import AdminCodexManager
from sloperator.config import Settings
from sloperator.store import EventStore


class FakeCodexServer:
    def __init__(self, *_args, **_kwargs) -> None:
        self.existing_thread_id: str | None = None
        self.closed = False

    async def start(self, existing_thread_id: str | None) -> str:
        self.existing_thread_id = existing_thread_id
        return existing_thread_id or "thread-new"

    async def run_turn(self, prompt: str) -> str:
        return f"answer: {prompt}"

    async def steer(self, _text: str) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


async def test_admin_codex_manager_runs_persists_and_deletes_turn(tmp_path: Path) -> None:
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
        session = await manager.create()
        assert await manager.submit(session["session_id"], "Inspect this") == "started"
        await manager._tasks[session["session_id"]]

    persisted = store.get_admin_codex_session(session["session_id"])
    assert persisted is not None
    assert persisted["title"] == "Inspect this"
    assert persisted["external_thread_id"] == "thread-new"
    assert persisted["status"] == "idle"
    assert [(item["role"], item["text"]) for item in persisted["messages"]] == [
        ("user", "Inspect this"),
        ("assistant", "answer: Inspect this"),
    ]
    assert await manager.delete(session["session_id"])
    assert store.get_admin_codex_session(session["session_id"]) is None
