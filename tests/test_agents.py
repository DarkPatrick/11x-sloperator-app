from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.agents import (
    AGENT_RETRY_DELAYS,
    CLAUDE_INITIAL_INSTRUCTION,
    FINAL_ARTIFACT_RECOVERY_PROMPT,
    INTERIM_RECOVERY_PROMPT,
    PATH_GUARD_RECOVERY_PROMPT,
    RESTART_RECOVERY_PROMPT,
    TIME_LIMIT_NOTICE,
    TIMEOUT_RECOVERY_FAILURE_NOTICE,
    TIMEOUT_RECOVERY_PROMPT,
    ActiveAgentRun,
    AgentAuthenticationError,
    AgentExecutionError,
    AgentOrchestrator,
    AgentRunResult,
    AgentTimeoutError,
    authentication_failure_notice,
    extract_artifact,
    fetch_thread_context,
    has_required_deliverable,
    is_authentication_failure,
    is_reply_path_guard_correction,
    optional_reply_instruction,
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


def test_reply_path_guard_correction_detection() -> None:
    correction = """\
:compass: Скилл: не использован
:books: Контекст: Graylog

✗ `/pro/tools/braintree/subscription` → (убрано — это HTTP-эндпоинт)
✗ `/webhooks/braintree` → (убрано — это значение поля uri)

Conclusions are unchanged.
"""

    assert is_reply_path_guard_correction(correction)
    assert not is_reply_path_guard_correction(
        "Причина установлена.\n\n- Проверили Graylog\n- События доставлены."
    )


def test_optional_reply_instruction_defaults_to_silence_and_forbids_local_links() -> None:
    prompt = optional_reply_instruction(
        "интересно",
        "[1.0] UOTHER: думаю, это сезонность\n[1.1] UOWNER: интересно",
        "UOWNER",
    )

    assert "Silence is the default" in prompt
    assert "talking to each other" in prompt
    assert "merely references/links to you" in prompt
    assert "Never mention or link local/server artifacts" in prompt
    assert "Do not propose edits to your own repository/project" in prompt
    assert "tag <@UOWNER>" in prompt
    assert "corrections to experiment results" in prompt
    assert "SLOPERATOR_NO_REPLY" in prompt
    assert "UOTHER: думаю, это сезонность" in prompt


@pytest.mark.asyncio
async def test_fetch_thread_context_reads_all_pages_and_all_authors() -> None:
    client = SimpleNamespace(
        conversations_replies=AsyncMock(
            side_effect=[
                {
                    "messages": [
                        {"ts": "1.0", "user": "UOTHER", "text": "Это не агенту"},
                    ],
                    "response_metadata": {"next_cursor": "next"},
                },
                {
                    "messages": [
                        {"ts": "1.1", "user": "UOWNER", "text": "Что думаешь ты?"},
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            ]
        )
    )

    context = await fetch_thread_context(client, "C123", "1.0")

    assert "UOTHER: Это не агенту" in context
    assert "UOWNER: Что думаешь ты?" in context
    assert client.conversations_replies.await_count == 2
    assert client.conversations_replies.await_args_list[1].kwargs["cursor"] == "next"


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


@pytest.mark.parametrize(
    "detail",
    (
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "authentication_failed",
        "initialize failed: 401 Unauthorized",
        "Please run 'codex login'",
    ),
)
def test_provider_authentication_failures_are_recognized(detail: str) -> None:
    assert is_authentication_failure(detail)


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


async def test_agent_timeout_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = AsyncMock(side_effect=AgentTimeoutError("limit reached"))
    sleep = AsyncMock()
    monkeypatch.setattr("sloperator.agents.asyncio.sleep", sleep)

    with pytest.raises(AgentTimeoutError, match="limit reached"):
        await retry_agent_service_errors(operation, context="automated trigger")

    operation.assert_awaited_once()
    sleep.assert_not_awaited()


async def test_agent_authentication_failure_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = AsyncMock(
        side_effect=AgentAuthenticationError("claude", "OAuth session expired")
    )
    sleep = AsyncMock()
    monkeypatch.setattr("sloperator.agents.asyncio.sleep", sleep)

    with pytest.raises(AgentAuthenticationError, match="OAuth session expired"):
        await retry_agent_service_errors(operation, context="automated trigger")

    operation.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.parametrize("provider", ("claude", "codex"))
async def test_slack_turn_reports_authentication_failure_immediately(
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = AsyncMock(side_effect=AgentAuthenticationError(provider, "credentials expired"))
    monkeypatch.setattr(f"sloperator.agents.run_{provider}", runner)
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    settings = Settings(
        slack_user_id="U1234567890",
        bot_token="xoxb-test",
        app_token="xapp-test",
        default_agent=provider,
        agent_workspace=tmp_path,
    )
    post_message = AsyncMock()
    client = SimpleNamespace(chat_postMessage=post_message, files_upload_v2=AsyncMock())
    orchestrator = AgentOrchestrator(settings, store)

    await orchestrator.submit(
        client,
        channel_id="C123",
        message_ts="100.1:analysis",
        thread_ts="100.1",
        text="Investigate",
        show_status=False,
        automated=True,
    )
    await orchestrator.drain()

    runner.assert_awaited_once()
    post_message.assert_awaited_once_with(
        channel="C123",
        thread_ts="100.1",
        markdown_text=authentication_failure_notice(provider, "U1234567890"),
    )
    session = store.get_agent_session("C123", "100.1")
    assert session is not None
    assert session.status == "failed"


async def test_headless_authentication_failure_immediately_dms_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_claude = AsyncMock(
        side_effect=AgentAuthenticationError("claude", "OAuth session expired")
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
    client = SimpleNamespace(
        conversations_open=AsyncMock(return_value={"channel": {"id": "D123"}}),
        chat_postMessage=AsyncMock(),
    )
    orchestrator = AgentOrchestrator(settings, store)
    orchestrator.set_notification_client(client)

    with pytest.raises(AgentAuthenticationError):
        await orchestrator.execute_once("Automated work", 5_400)

    run_claude.assert_awaited_once()
    client.conversations_open.assert_awaited_once_with(users="U1234567890")
    client.chat_postMessage.assert_awaited_once_with(
        channel="D123",
        markdown_text=authentication_failure_notice("claude", "U1234567890"),
    )


async def test_headless_run_can_override_agent_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_claude = AsyncMock(
        return_value=AgentRunResult(session_id="session-1", text="done")
    )
    monkeypatch.setattr("sloperator.agents.run_claude", run_claude)
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    default_workspace = tmp_path / "default"
    audit_workspace = tmp_path / "audit"
    settings = Settings(
        slack_user_id="U1234567890",
        bot_token="xoxb-test",
        app_token="xapp-test",
        agent_workspace=default_workspace,
    )
    orchestrator = AgentOrchestrator(settings, store)

    await orchestrator.execute_once(
        "[claude:opus] Audit",
        5_400,
        workspace=audit_workspace,
    )

    run_settings = run_claude.await_args.args[0]
    assert run_settings.agent_workspace == audit_workspace
    assert orchestrator.settings.agent_workspace == default_workspace


async def test_automated_trigger_returns_partial_result_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_claude = AsyncMock(
        side_effect=(
            AgentTimeoutError("limit reached"),
            AgentRunResult(session_id="session-1", text="Partial verified finding"),
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
    client = SimpleNamespace(chat_postMessage=post_message, files_upload_v2=AsyncMock())
    orchestrator = AgentOrchestrator(settings, store)

    await orchestrator.submit(
        client,
        channel_id="C123",
        message_ts="100.1:analysis",
        thread_ts="100.1",
        text="Investigate",
        show_status=False,
        automated=True,
    )
    await orchestrator.drain()

    assert run_claude.await_count == 2
    assert run_claude.await_args.args[2] == TIMEOUT_RECOVERY_PROMPT
    assert run_claude.await_args.kwargs["force_resume"] is True
    post_message.assert_awaited_once_with(
        channel="C123",
        thread_ts="100.1",
        markdown_text=f"{TIME_LIMIT_NOTICE}\n\nPartial verified finding",
    )


async def test_automated_trigger_keeps_session_resumable_when_partial_recovery_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_claude = AsyncMock(side_effect=AgentTimeoutError("limit reached"))
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
    client = SimpleNamespace(chat_postMessage=post_message, files_upload_v2=AsyncMock())
    orchestrator = AgentOrchestrator(settings, store)

    await orchestrator.submit(
        client,
        channel_id="C123",
        message_ts="100.1:analysis",
        thread_ts="100.1",
        text="Investigate",
        show_status=False,
        automated=True,
    )
    await orchestrator.drain()

    assert run_claude.await_count == 2
    post_message.assert_awaited_once_with(
        channel="C123",
        thread_ts="100.1",
        markdown_text=TIMEOUT_RECOVERY_FAILURE_NOTICE.strip(),
    )
    session = store.get_agent_session("C123", "100.1")
    assert session is not None
    assert session.status == "idle"
    assert session.external_session_id


async def test_interrupted_automated_turn_resumes_same_session_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_claude = AsyncMock(
        return_value=AgentRunResult(session_id="existing-session", text="Recovered result")
    )
    monkeypatch.setattr("sloperator.agents.run_claude", run_claude)
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    store.prepare_agent_request("C123", "100.1:analysis", "100.1")
    store.create_agent_session(
        "C123", "100.1", "claude", "opus", "existing-session"
    )
    store.cancel_agent_turn("C123", "100.1")
    store.save_durable_agent_run(
        "C123",
        "100.1:analysis",
        "100.1",
        "Original expensive investigation",
        {
            "show_status": False,
            "timeout_seconds": 3_600,
            "disable_link_previews": False,
            "optional_reply": False,
            "require_artifact": False,
            "automated": True,
        },
    )
    store.set_durable_agent_run_status("C123", "100.1:analysis", "interrupted")
    settings = Settings(
        slack_user_id="U1234567890",
        bot_token="xoxb-test",
        app_token="xapp-test",
        agent_workspace=tmp_path,
    )
    post_message = AsyncMock()
    client = SimpleNamespace(chat_postMessage=post_message, files_upload_v2=AsyncMock())
    orchestrator = AgentOrchestrator(settings, store)

    assert await orchestrator.resume_interrupted(client) == 1
    await orchestrator.drain()

    prompt = run_claude.await_args.args[2]
    assert RESTART_RECOVERY_PROMPT in prompt
    assert "Original expensive investigation" in prompt
    assert run_claude.await_args.args[1].external_session_id == "existing-session"
    post_message.assert_awaited_once_with(
        channel="C123", thread_ts="100.1", markdown_text="Recovered result"
    )
    assert store.list_interrupted_durable_agent_runs() == []


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


async def test_headless_run_ignores_interim_result_and_continues(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_claude = AsyncMock(
        side_effect=(
            AgentRunResult(session_id="session-1", text="Work is still running"),
            AgentRunResult(session_id="session-1", text="Final notification"),
        )
    )
    monkeypatch.setattr("sloperator.agents.run_claude", run_claude)
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    orchestrator = AgentOrchestrator(settings, store)

    result = await orchestrator.execute_once(
        "Automated work",
        5_400,
        accept_result=lambda text: text == "Final notification",
    )

    assert run_claude.await_count == 2
    assert run_claude.await_args_list[1].args[2] == INTERIM_RECOVERY_PROMPT
    assert result.text == "Final notification"
    assert store.list_scheduled_agent_runs()[0]["messages"][-1]["text"] == (
        "Final notification"
    )


async def test_headless_timeout_returns_partial_failure_without_retry(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_claude = AsyncMock(
        side_effect=(
            AgentTimeoutError("limit reached"),
            AgentRunResult(session_id="session-1", text="Partial calculation details"),
        )
    )
    monkeypatch.setattr("sloperator.agents.run_claude", run_claude)
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    orchestrator = AgentOrchestrator(settings, store)

    result = await orchestrator.execute_once("Automated work", 5_400)

    assert run_claude.await_count == 2
    assert run_claude.await_args.args[2] == TIMEOUT_RECOVERY_PROMPT
    assert result.text == (
        "Experiment finalisation failed: agent exhausted its work-time limit. "
        "Partial calculation details"
    )


async def test_interrupted_headless_turn_resumes_original_session(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_claude = AsyncMock(
        return_value=AgentRunResult(
            session_id="scheduled-session", text="Recovered scheduled result"
        )
    )
    monkeypatch.setattr("sloperator.agents.run_claude", run_claude)
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    store.create_scheduled_agent_run(
        "run-1", "experiment-finalizer", "claude", "opus", "scheduled-session",
        "Original cron request"
    )
    store.finish_scheduled_agent_run("run-1", status="interrupted")
    orchestrator = AgentOrchestrator(settings, store)
    orchestrator._active_runs[("scheduled", "run-1")] = ActiveAgentRun("claude")
    # A retained process-control object is not proof that work is still active.
    assert ("scheduled", "run-1") not in orchestrator.active_keys()
    orchestrator._active_runs.clear()

    recovered = await orchestrator.resume_interrupted_headless(5_400)

    assert len(recovered) == 1
    assert recovered[0].run_id == "run-1"
    assert recovered[0].text == "Recovered scheduled result"
    assert run_claude.await_args.args[1].external_session_id == "scheduled-session"
    assert run_claude.await_args.kwargs["force_resume"] is True
    assert RESTART_RECOVERY_PROMPT in run_claude.await_args.args[2]
    pending_publication = store.list_interrupted_scheduled_agent_runs()
    assert pending_publication[0]["status"] == "recovered"
    assert pending_publication[0]["result_text"] == "Recovered scheduled result"


async def test_interrupted_headless_turn_ignores_interim_result_and_continues(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_claude = AsyncMock(
        side_effect=(
            AgentRunResult(session_id="scheduled-session", text="Calculation is still running"),
            AgentRunResult(session_id="scheduled-session", text="Final notification"),
        )
    )
    monkeypatch.setattr("sloperator.agents.run_claude", run_claude)
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    store.create_scheduled_agent_run(
        "run-1", "experiment-finalizer", "claude", "opus", "scheduled-session",
        "Original cron request"
    )
    store.finish_scheduled_agent_run("run-1", status="interrupted")
    orchestrator = AgentOrchestrator(settings, store)

    recovered = await orchestrator.resume_interrupted_headless(
        5_400,
        accept_result=lambda text: text == "Final notification",
    )

    assert run_claude.await_count == 2
    assert run_claude.await_args_list[1].args[2] == INTERIM_RECOVERY_PROMPT
    assert recovered[0].text == "Final notification"
    assert store.list_interrupted_scheduled_agent_runs()[0]["result_text"] == (
        "Final notification"
    )


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


def test_reused_analysis_satisfies_artifact_contract_and_becomes_slack_link(
    tmp_path: Path,
) -> None:
    url = "https://ultimate-guitar.slack.com/archives/C0AJKHFHVHV/p1234567890123456"

    response, artifact = extract_artifact(
        f"<@UOWNER>\nSLOPERATOR_REUSE_ANALYSIS: {url}",
        tmp_path,
    )

    assert has_required_deliverable(f"SLOPERATOR_REUSE_ANALYSIS: {url}")
    assert response == f"<@UOWNER>\nПовтор этого же алерта — [открыть существующий разбор]({url})."
    assert artifact is None


def test_reused_analysis_rejects_non_slack_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="некорректную Slack-ссылку"):
        extract_artifact(
            "SLOPERATOR_REUSE_ANALYSIS: https://example.com/not-slack",
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


async def test_slack_turn_recovers_from_reply_path_guard_correction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    correction = (
        ":compass: Скилл: не использован\n"
        "✗ `/webhooks/braintree` → (убрано — это HTTP-эндпоинт)\n"
        "Conclusions are unchanged."
    )
    run_claude = AsyncMock(
        side_effect=(
            AgentRunResult(session_id="session-1", text=correction),
            AgentRunResult(session_id="session-1", text="Полный исправленный ответ"),
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
    client = SimpleNamespace(chat_postMessage=post_message, files_upload_v2=AsyncMock())
    orchestrator = AgentOrchestrator(settings, store)

    await orchestrator.submit(
        client,
        channel_id="C123",
        message_ts="100.1:followup",
        thread_ts="100.1",
        text="Уточни вывод",
        show_status=False,
    )
    await orchestrator.drain()

    assert run_claude.await_count == 2
    assert run_claude.await_args.args[2] == PATH_GUARD_RECOVERY_PROMPT
    assert run_claude.await_args.kwargs["force_resume"] is True
    post_message.assert_awaited_once_with(
        channel="C123",
        thread_ts="100.1",
        markdown_text="Полный исправленный ответ",
    )
