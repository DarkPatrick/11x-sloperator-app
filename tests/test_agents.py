from __future__ import annotations

import pytest

from sloperator.agents import parse_agent_request, split_slack_message, thread_key
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


def test_split_slack_message_preserves_all_text() -> None:
    text = "first paragraph\n\n" + ("x" * 100)

    chunks = split_slack_message(text, limit=40)

    assert all(len(chunk) <= 40 for chunk in chunks)
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")
