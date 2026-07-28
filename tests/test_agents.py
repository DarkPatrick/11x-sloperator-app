from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.agents import (
    AGENT_RETRY_DELAYS,
    AgentExecutionError,
    AgentOrchestrator,
    extract_artifact,
    parse_agent_request,
    retry_agent_service_errors,
    split_slack_message,
    thread_key,
)
from sloperator.config import Settings


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
