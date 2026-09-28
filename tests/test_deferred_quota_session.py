from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.agents import AgentOrchestrator, AgentRunResult
from sloperator.config import Settings
from sloperator.store import EventStore


@pytest.mark.asyncio
async def test_deferred_turn_creates_resumable_session_before_waiting_for_quota(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        agent_workspace=tmp_path,
    )
    gate_entered = asyncio.Event()
    gate_open = asyncio.Event()

    async def wait_for_admission(*args, **kwargs) -> None:
        session = store.get_agent_session("D123", "100.1")
        assert session is not None
        assert session.status == "idle"
        gate_entered.set()
        await gate_open.wait()

    monkeypatch.setattr(
        "sloperator.agents.wait_for_automated_quota_admission",
        wait_for_admission,
    )
    run_claude = AsyncMock(
        return_value=AgentRunResult(session_id="provider-session", text="- Готово")
    )
    monkeypatch.setattr("sloperator.agents.run_claude", run_claude)
    client = SimpleNamespace(chat_postMessage=AsyncMock(), files_upload_v2=AsyncMock())
    orchestrator = AgentOrchestrator(settings, store)

    await orchestrator.submit(
        client,
        channel_id="D123",
        message_ts="100.1",
        thread_ts="100.1",
        text="Собери дайджест",
        show_status=False,
        automated=True,
        react_to_message=False,
        agent_name="daily-activity-digest/slack",
        wait_for_quota_admission=True,
    )
    await asyncio.wait_for(gate_entered.wait(), timeout=1)

    session = store.get_agent_session("D123", "100.1")
    assert session is not None
    assert session.agent_name == "daily-activity-digest/slack"
    assert store.has_agent_thread("D123", "100.1")
    run_claude.assert_not_awaited()

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE agent_sessions SET last_activity_at = '2000-01-01 00:00:00' "
            "WHERE channel_id = 'D123' AND thread_ts = '100.1'"
        )
    before_reply_activity = dt.datetime(2000, 1, 1)

    gate_open.set()
    await orchestrator.drain()

    run_claude.assert_awaited_once()
    client.chat_postMessage.assert_awaited_once_with(
        channel="D123",
        thread_ts="100.1",
        markdown_text="- Готово",
    )
    completed = next(
        row
        for row in store.list_agent_sessions()
        if row["channel_id"] == "D123" and row["thread_ts"] == "100.1"
    )
    assert dt.datetime.fromisoformat(completed["last_activity_at"]) >= before_reply_activity
