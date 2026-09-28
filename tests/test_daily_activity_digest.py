from __future__ import annotations

import datetime as dt
from dataclasses import replace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from sloperator.agents import HeadlessAgentRun
from sloperator.claude_usage import ClaudeUsage
from sloperator.config import Settings
from sloperator.daily_activity_digest import (
    DIGEST_PROMPT,
    DigestFacts,
    DigestItem,
    _run_task_keys,
    build_digest,
    is_valid_agent_digest,
    next_run_at,
    render_fallback,
    run_once,
)


def settings(tmp_path) -> Settings:
    return Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-token",
        app_token="xapp-token",
        database_path=tmp_path / "db.sqlite3",
    )


def facts() -> DigestFacts:
    return DigestFacts(
        completed=(
            DigestItem(
                "UMN-1",
                "Сбор схем планов",  # noqa: RUF001
                "https://mu--se.atlassian.net/browse/UMN-1",
                "подготовлен документ",
                "https://alice.mu.se/pages/123",
            ),
        ),
        pending=(
            DigestItem(
                "UMN-2",
                "Аналитика оплаты",
                "https://mu--se.atlassian.net/browse/UMN-2",
                "первая в очереди; возьму в работу завтра",
            ),
        ),
    )


def test_next_run_is_weekday_and_preserves_cyprus_wall_clock() -> None:
    friday_evening = dt.datetime(2026, 10, 2, 18, tzinfo=dt.UTC)

    result = next_run_at(friday_evening)

    assert result == dt.datetime(2026, 10, 5, 19, tzinfo=ZoneInfo("Asia/Nicosia"))


def test_fallback_embeds_links_in_meaningful_text() -> None:
    message = render_fallback(facts())

    assert (
        "[Сбор схем планов](https://mu--se.atlassian.net/browse/UMN-1)"  # noqa: RUF001
        in message
    )
    assert "[подготовлен документ](https://alice.mu.se/pages/123)" in message
    assert "Jira:" not in message
    assert "Confluence:" not in message


def test_agent_validation_requires_all_embedded_links() -> None:
    draft = render_fallback(facts())

    assert is_valid_agent_digest(draft, draft)
    assert not is_valid_agent_digest(draft.replace("[подготовлен документ]", "документ "), draft)
    assert not is_valid_agent_digest(draft.replace("https://alice.mu.se/pages/123", ""), draft)
    assert not is_valid_agent_digest(draft + "\n- Продолжим завтра.", draft)


def test_run_task_key_ignores_incidental_links_from_context() -> None:
    run = {
        "channel_name": "jira-task-worker",
        "messages": [
            {
                "text": (
                    "Context mentions UMN-13460. You are the worker for Jira task UMN-13560. "
                    "Another example is UMN-12000."
                )
            }
        ],
    }

    assert _run_task_keys(run) == ("UMN-13560",)


@pytest.mark.asyncio
async def test_low_quota_uses_fallback_without_agent(monkeypatch, tmp_path) -> None:
    current_facts = replace(facts(), quota_note="Лимита для полного запуска недостаточно.")
    monkeypatch.setattr(
        "sloperator.daily_activity_digest.collect_facts",
        AsyncMock(return_value=(current_facts, ClaudeUsage(60, 10, "", "Oct 2, 10:00PM"))),
    )
    agent = AsyncMock()

    message, used_agent = await build_digest(settings(tmp_path), AsyncMock(), agent)

    assert not used_agent
    assert "Лимита" in message
    agent.execute_once.assert_not_called()


@pytest.mark.asyncio
async def test_agent_failure_uses_fallback(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "sloperator.daily_activity_digest.collect_facts",
        AsyncMock(return_value=(facts(), ClaudeUsage(0, 0, "", "Oct 2, 10:00PM"))),
    )
    agent = AsyncMock()
    agent.execute_once.side_effect = RuntimeError("provider unavailable")

    message, used_agent = await build_digest(settings(tmp_path), AsyncMock(), agent)

    assert not used_agent
    assert message == render_fallback(facts())


@pytest.mark.asyncio
async def test_successful_agent_digest_is_sent_to_owner(monkeypatch, tmp_path) -> None:
    draft = render_fallback(facts())
    monkeypatch.setattr(
        "sloperator.daily_activity_digest.collect_facts",
        AsyncMock(return_value=(facts(), ClaudeUsage(0, 0, "", "Oct 2, 10:00PM"))),
    )
    agent = AsyncMock()
    agent.execute_once.return_value = HeadlessAgentRun(
        "claude", "claude-opus-5-5", "session", draft
    )
    client = AsyncMock()
    client.conversations_open.return_value = {"channel": {"id": "D123"}}

    message, used_agent = await run_once(client, agent, settings(tmp_path), AsyncMock())

    assert used_agent
    assert message == draft
    assert DIGEST_PROMPT in agent.execute_once.await_args.args[0]
    assert "AUTOMATED RESPONSE STYLE" in agent.execute_once.await_args.args[0]
    client.conversations_open.assert_awaited_once_with(users="UOWNER")
    assert client.chat_postMessage.await_args.kwargs["channel"] == "D123"
