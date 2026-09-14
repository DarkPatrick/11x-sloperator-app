import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.agents import (
    ActiveAgentRun,
    AgentOrchestrator,
    retry_agent_service_errors,
    run_claude,
)
from sloperator.claude_budget import ClaudeBudgetExceeded, ClaudeQuotaExceeded, TranscriptBudget
from sloperator.config import Settings
from sloperator.store import AgentSession, EventStore


def record(path, identifier="message", inputs=1, cached=0, created=0, outputs=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": identifier,
                        "usage": {
                            "input_tokens": inputs,
                            "cache_read_input_tokens": cached,
                            "cache_creation_input_tokens": created,
                            "output_tokens": outputs,
                        },
                    },
                }
            )
            + "\n"
        )


def test_budget_counts_cached_context_children_and_partial_updates(tmp_path):
    root = tmp_path / "session.jsonl"
    record(root, inputs=10, cached=20, created=30, outputs=2)
    record(root, inputs=10, cached=20, created=30, outputs=4)
    budget = TranscriptBudget(tmp_path, "session", input_limit=100, output_limit=10)
    budget.check()
    budget.check()  # Re-reading and repeated message IDs must not count twice.
    record(tmp_path / "session/subagents/agent-a.jsonl", cached=39)
    with pytest.raises(ClaudeBudgetExceeded, match="input=100/100"):
        budget.check()


async def test_resume_cannot_reset_consumed_budget(tmp_path):
    record(tmp_path / "session.jsonl", outputs=20)
    operation = AsyncMock()
    budget = TranscriptBudget(tmp_path, "session", input_limit=100, output_limit=20)
    with pytest.raises(ClaudeBudgetExceeded):
        await budget.run(operation)
    operation.assert_not_awaited()


async def test_live_budget_cancels_running_provider(tmp_path):
    cancelled = asyncio.Event()

    async def provider():
        record(tmp_path / "session/subagents/agent-child.jsonl", outputs=21)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    budget = TranscriptBudget(tmp_path, "session", input_limit=100, output_limit=20)
    with pytest.raises(ClaudeBudgetExceeded):
        await budget.run(provider, interval=0.01)
    assert cancelled.is_set()


@pytest.mark.parametrize("error", [ClaudeBudgetExceeded("budget"), ClaudeQuotaExceeded("quota")])
async def test_spending_stops_are_never_retried(error):
    operation = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await retry_agent_service_errors(operation, context="test", delays=(0, 0))
    operation.assert_awaited_once()


async def test_provider_quota_response_is_terminal(tmp_path, monkeypatch):
    settings = Settings(
        slack_user_id="U123", bot_token="test", app_token="test", agent_workspace=tmp_path
    )
    session = AgentSession(
        channel_id="C123",
        thread_ts="1",
        provider="claude",
        model="opus",
        external_session_id="session",
        status="idle",
        turn_count=1,
        last_error=None,
    )
    monkeypatch.setattr(
        "sloperator.agents._run_process",
        AsyncMock(
            return_value=(
                1,
                json.dumps({"is_error": True, "result": "You've hit your session limit"}),
                "",
            )
        ),
    )
    with pytest.raises(ClaudeQuotaExceeded):
        await run_claude(settings, session, "Investigate", ActiveAgentRun("claude"))


async def test_run_claude_enforces_budget_before_launch(tmp_path, monkeypatch):
    settings = Settings(
        slack_user_id="U123",
        bot_token="test",
        app_token="test",
        agent_workspace=tmp_path,
        automated_claude_output_budget=10,
    )
    session = AgentSession(
        channel_id="C123",
        thread_ts="1",
        provider="claude",
        model="opus",
        external_session_id="session",
        status="idle",
        turn_count=1,
        last_error=None,
    )
    record(tmp_path / "session.jsonl", outputs=10)
    monkeypatch.setattr("sloperator.agents.transcript_directory", lambda _: tmp_path)
    process = AsyncMock()
    monkeypatch.setattr("sloperator.agents._run_process", process)
    control = ActiveAgentRun("claude")
    control.enforce_claude_budget = True
    with pytest.raises(ClaudeBudgetExceeded):
        await run_claude(settings, session, "Investigate", control)
    process.assert_not_awaited()


async def test_quota_stop_during_startup_does_not_crash_service(tmp_path, monkeypatch):
    settings = Settings(
        slack_user_id="U123", bot_token="test", app_token="test", agent_workspace=tmp_path
    )
    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.create_scheduled_agent_run(
        "old", "experiment-config-check", "claude", "opus", "session", "Original request"
    )
    store.finish_scheduled_agent_run("old", status="interrupted")
    runner = AsyncMock(side_effect=ClaudeQuotaExceeded("quota"))
    monkeypatch.setattr("sloperator.agents.run_claude", runner)
    orchestrator = AgentOrchestrator(settings, store)
    assert await orchestrator.resume_interrupted_headless(60) == []
    assert store.list_interrupted_scheduled_agent_runs() == []
    assert orchestrator.active_keys() == set()
    runner.assert_awaited_once()


@pytest.mark.parametrize("error", [ClaudeBudgetExceeded("budget"), ClaudeQuotaExceeded("quota")])
async def test_automated_slack_stop_is_terminal_and_explained(tmp_path, monkeypatch, error):
    settings = Settings(
        slack_user_id="U123", bot_token="test", app_token="test", agent_workspace=tmp_path
    )
    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    runner = AsyncMock(side_effect=error)
    monkeypatch.setattr("sloperator.agents.run_claude", runner)
    orchestrator = AgentOrchestrator(settings, store)
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await orchestrator.submit(
        client,
        channel_id="C123",
        thread_ts="1.1",
        message_ts="1.1",
        text="Investigate",
        show_status=False,
        automated=True,
    )
    await orchestrator.drain()
    runner.assert_awaited_once()
    assert runner.call_args.args[3].enforce_claude_budget
    assert store.get_agent_session("C123", "1.1").status == "failed"
    assert store.list_interrupted_durable_agent_runs() == []
    assert "остановлен" in client.chat_postMessage.call_args.kwargs["markdown_text"]
