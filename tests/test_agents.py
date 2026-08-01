from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.agents import (
    AGENT_RETRY_DELAYS,
    CLAUDE_INITIAL_INSTRUCTION,
    FINAL_ARTIFACT_RECOVERY_PROMPT,
    AgentExecutionError,
    AgentOrchestrator,
    AgentRunResult,
    extract_artifact,
    parse_agent_request,
    retry_agent_service_errors,
    split_slack_message,
    thread_key,
)
from sloperator.config import Settings
from sloperator.store import EventStore


def test_claude_initial_instruction_references_claude_md() -> None:
    assert "CLAUDE.md" in CLAUDE_INITIAL_INSTRUCTION
    assert "AGENTS.md" not in CLAUDE_INITIAL_INSTRUCTION


@pytest.fixture
def settings() -> Settings:
    return Settings(
        slack_user_id="U1234567890",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )


def test_parse_agent_request_uses_default(settings: Settings) -> None:
    request = parse_agent_request("Посчитай эксперимент", settings)

    assert request.provider == "claude"
    assert request.model == "opus"
    assert request.prompt == "Посчитай эксперимент"


def test_parse_agent_request_supports_provider_and_model(settings: Settings) -> None:
    request = parse_agent_request("[codex:gpt-5.6-sol] Проверь код", settings)

    assert request.provider == "codex"
    assert request.model == "gpt-5.6-sol"
    assert request.prompt == "Проверь код"


def test_parse_agent_request_rejects_empty_prompt(settings: Settings) -> None:
    with pytest.raises(ValueError, match="написать запрос"):
        parse_agent_request("[claude:opus]", settings)


def test_thread_key_starts_new_session_for_top_level_message() -> None:
    assert thread_key("100.1", None) == "100.1"
    assert thread_key("100.2", "100.1") == "100.1"


def test_agent_service_retry_delays_grow_to_one_hour() -> None:
    assert AGENT_RETRY_DELAYS == (60, 300, 900, 1_800, 3_600)


async def test_agent_service_errors_retry_with_progressive_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = AsyncMock(
        side_effect=(
            AgentExecutionError("overloaded"),
            AgentExecutionError("service unavailable"),
            "completed",
        )
    )
    sleep = AsyncMock()
    monkeypatch.setattr("sloperator.agents.asyncio.sleep", sleep)

    result = await retry_agent_service_errors(
        operation,
        context="test turn",
        delays=(15, 30, 60),
    )

    assert result == "completed"
    assert operation.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [15, 30]


async def test_agent_service_error_is_raised_only_after_all_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = AsyncMock(side_effect=AgentExecutionError("unavailable"))
    sleep = AsyncMock()
    monkeypatch.setattr("sloperator.agents.asyncio.sleep", sleep)

    with pytest.raises(AgentExecutionError, match="unavailable"):
        await retry_agent_service_errors(
            operation,
            context="test turn",
            delays=(15, 30),
        )

    assert operation.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [15, 30]


async def test_headless_run_is_visible_and_disables_interactive_hooks(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_claude = AsyncMock(return_value=AgentRunResult(session_id="session-1", text="Final result"))
    monkeypatch.setattr("sloperator.agents.run_claude", run_claude)
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    orchestrator = AgentOrchestrator(settings, store)

    result = await orchestrator.execute_once("Automated work", 5_400)

    assert result.text == "Final result"
    assert result.run_id is not None
    assert run_claude.await_args.kwargs["environment_overrides"] == {"UG_SKIP_PREFLIGHT": "1"}
    sessions = orchestrator.headless_sessions()
    assert len(sessions) == 1
    assert sessions[0]["headless"] is True
    assert sessions[0]["status"] == "completed"
    assert sessions[0]["active"] is False
    persisted = store.list_scheduled_agent_runs()
    assert persisted[0]["thread_ts"] == result.run_id
    assert [message["text"] for message in persisted[0]["messages"]] == [
        "Automated work",
        "Final result",
    ]

    await orchestrator.attach_session("D123", "100.1", result)

    assert orchestrator.headless_sessions() == []
    assert store.list_scheduled_agent_runs()[0]["thread_ts"] == result.run_id
    assert store.get_agent_session("D123", "100.1") is not None


def test_split_slack_message_preserves_all_text() -> None:
    text = "first paragraph\n\n" + ("x" * 100)

    chunks = split_slack_message(text, limit=40)

    assert all(len(chunk) <= 40 for chunk in chunks)
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")


async def test_agent_replies_use_standard_markdown_parameter(settings: Settings) -> None:
    post_message = AsyncMock()
    client = SimpleNamespace(chat_postMessage=post_message)
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.settings = settings

    await orchestrator._reply(
        client,
        channel_id="C123",
        thread_ts="100.1",
        text="## Result\n\n**Formatted**",
    )

    post_message.assert_awaited_once_with(
        channel="C123",
        thread_ts="100.1",
        markdown_text="## Result\n\n**Formatted**",
    )


async def test_agent_reply_can_disable_link_previews(settings: Settings) -> None:
    post_message = AsyncMock()
    client = SimpleNamespace(chat_postMessage=post_message)
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.settings = settings

    await orchestrator._reply(
        client,
        channel_id="D123",
        thread_ts="100.1",
        text="[Project](https://example.com)",
        disable_link_previews=True,
    )

    post_message.assert_awaited_once_with(
        channel="D123",
        thread_ts="100.1",
        markdown_text="[Project](https://example.com)",
        unfurl_links=False,
        unfurl_media=False,
    )


def test_extract_artifact_removes_and_validates_marker(tmp_path) -> None:
    artifact = tmp_path / "output" / "analysis.zip"
    artifact.parent.mkdir()
    artifact.write_bytes(b"zip")

    response, extracted = extract_artifact(
        "Finding\n\nSLOPERATOR_ARTIFACT: output/analysis.zip",
        tmp_path,
    )

    assert response == "Finding"
    assert extracted == artifact


def test_extract_artifact_rejects_paths_outside_workspace(tmp_path) -> None:
    with pytest.raises(ValueError, match="за пределами"):
        extract_artifact(
            "SLOPERATOR_ARTIFACT: ../analysis.zip",
            tmp_path,
        )


async def test_agent_reply_uploads_artifact_to_same_thread(tmp_path) -> None:
    artifact = tmp_path / "output" / "analysis.zip"
    artifact.parent.mkdir()
    artifact.write_bytes(b"zip")
    post_message = AsyncMock()
    upload = AsyncMock()
    client = SimpleNamespace(chat_postMessage=post_message, files_upload_v2=upload)
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.settings = Settings(
        slack_user_id="U1234567890",
        bot_token="xoxb-test",
        app_token="xapp-test",
        agent_workspace=tmp_path,
    )

    await orchestrator._reply(
        client,
        channel_id="C123",
        thread_ts="100.1",
        text="Finding\nSLOPERATOR_ARTIFACT: output/analysis.zip",
    )

    post_message.assert_awaited_once_with(
        channel="C123",
        thread_ts="100.1",
        markdown_text="Finding",
    )
    upload.assert_awaited_once_with(
        channel="C123",
        thread_ts="100.1",
        file=artifact,
        filename="analysis.zip",
        title="Артефакты анализа",
    )


async def test_required_artifact_recovers_from_internal_claude_review_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "output" / "analysis.zip"
    artifact.parent.mkdir()
    artifact.write_bytes(b"zip")
    run_claude = AsyncMock(
        side_effect=(
            AgentRunResult(
                session_id="session-1",
                text="Round-2 approval completed; nothing changes.",
            ),
            AgentRunResult(
                session_id="session-1",
                text="Actual finding\nSLOPERATOR_ARTIFACT: output/analysis.zip",
            ),
        )
    )
    monkeypatch.setattr("sloperator.agents.run_claude", run_claude)
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    settings = Settings(
        slack_user_id="U1234567890",
        bot_token="xoxb-test",
        app_token="xapp-test",
        agent_workspace=tmp_path,
    )
    post_message = AsyncMock()
    upload = AsyncMock()
    client = SimpleNamespace(chat_postMessage=post_message, files_upload_v2=upload)
    orchestrator = AgentOrchestrator(settings, store)

    await orchestrator.submit(
        client,
        channel_id="C123",
        message_ts="100.1:analysis",
        thread_ts="100.1",
        text="Investigate",
        show_status=False,
        require_artifact=True,
    )
    await orchestrator.drain()

    assert run_claude.await_count == 2
    assert run_claude.await_args.args[2] == FINAL_ARTIFACT_RECOVERY_PROMPT
    assert run_claude.await_args.kwargs["force_resume"] is True
    post_message.assert_awaited_once_with(
        channel="C123",
        thread_ts="100.1",
        markdown_text="Actual finding",
    )
    upload.assert_awaited_once()
