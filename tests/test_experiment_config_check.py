from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.config import Settings
from sloperator.experiment_config_check import (
    ExperimentConfigResponder,
    build_experiment_config_prompt,
    build_notification_intro,
    experiment_config_payload,
    extract_project_links_and_clean_body,
    format_slack_mrkdwn,
    is_experiment_config_trigger,
    normalize_experiment_config_result,
)


def _event() -> dict[str, object]:
    return {
        "channel": "DSTARTER",
        "channel_type": "im",
        "bot_id": "BSELF",
        "ts": "100.1",
        "text": "Automated check",
        "metadata": {
            "event_type": "ug_experiment_config_check",
            "event_payload": {
                "recipient_id": "USTARTER",
                "experiments": [{"id": 7890, "name": "[UG Monetization] Test"}],
            },
        },
    }


def test_trigger_requires_top_level_bot_dm_and_valid_metadata() -> None:
    event = _event()

    assert is_experiment_config_trigger(event)
    assert not is_experiment_config_trigger({**event, "channel_type": "channel"})
    assert not is_experiment_config_trigger({**event, "bot_id": None})
    assert not is_experiment_config_trigger({**event, "thread_ts": "99.1"})
    assert experiment_config_payload({**event, "metadata": {}}) is None


def test_payload_accepts_slack_stringified_integer_metadata() -> None:
    event = _event()
    event["metadata"]["event_payload"]["experiments"][0]["id"] = "7890"  # type: ignore[index]

    payload = experiment_config_payload(event)

    assert payload is not None
    assert payload["experiments"][0]["id"] == 7890


def test_prompt_makes_every_requested_check_and_is_read_only() -> None:
    payload = experiment_config_payload(_event())
    assert payload is not None

    prompt = build_experiment_config_prompt(payload, interactive=False)

    assert "ug-experiment-config-builder" in prompt
    assert "application versions" in prompt
    assert "activation event" in prompt
    assert "number and identifiers of test branches" in prompt
    assert "Нужно исправить" in prompt
    assert "do not mention app versions at all" in prompt
    assert "do not print or discuss them" in prompt
    assert "Uncertainty means silence" in prompt
    assert "not authorised" in prompt
    assert "contact the monetisation-team analysts" in prompt
    assert "read-only audit" in prompt
    assert "AUTOMATED RESPONSE STYLE" in prompt


def test_result_without_verdict_is_issues_only_when_issue_sections_exist() -> None:
    assert normalize_experiment_config_result(
        "🧭 Скилл: test\n📚 Контекст: test\n**Нужно исправить**\nWrong event."
    ) == (
        "ISSUES",
        "**Нужно исправить**\nWrong event.",
    )
    with pytest.raises(ValueError, match="has no experiment-config verdict"):
        normalize_experiment_config_result("Проверка завершена без структурированного вывода")


def test_result_rejects_forbidden_low_value_content() -> None:
    with pytest.raises(ValueError, match="forbidden low-value content"):
        normalize_experiment_config_result(
            "**Нужно исправить**\nДата окончания не проставлена.\n"  # noqa: RUF001
            "EXPERIMENT_CONFIG_VERDICT: ISSUES"
        )


def test_slack_formatter_uses_mrkdwn_and_plain_code_fences() -> None:
    assert format_slack_mrkdwn("**Нужно исправить**\n```text\nsegments: {}\n```") == (
        "*Нужно исправить*\n```\nsegments: {}\n```"
    )


def test_notification_intro_mentions_starter_and_experiment() -> None:
    intro = build_notification_intro(
        "USTARTER",
        [{"id": 7890, "name": "[UG Monetization] Test"}],
        ["https://alice.mu.se/pages/123"],
    )

    assert intro.startswith(":wave: Привет, <@USTARTER>! Ты недавно запустил эксперимент")
    assert "<https://alice.mu.se/pages/123|«[UG Monetization] Test»>" in intro
    assert (
        "<https://www.ultimate-guitar.com/components/ab/experiment/view?id=7890|id 7890>"
        in intro
    )


def test_project_links_move_to_intro_and_identity_lines_leave_body() -> None:
    projects, body = extract_project_links_and_clean_body(
        "**[UG Monetization] Test** (id 7890)\n"
        "Проект: https://alice.mu.se/pages/123\n"
        "Админка: https://example.test/7890\n\n"
        "**Нужно исправить**\nWrong event.",
        [{"id": 7890, "name": "[UG Monetization] Test"}],
    )

    assert projects == ["https://alice.mu.se/pages/123"]
    assert body == "**Нужно исправить**\nWrong event."


@pytest.mark.asyncio
async def test_responder_launches_durable_thread_with_access_specific_closing() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        slack_allowed_conversation_users=frozenset({"UOWNER", "USTARTER"}),
    )
    agent = AsyncMock()
    client = AsyncMock()

    await ExperimentConfigResponder(settings, agent).handle(_event(), client)

    kwargs = agent.submit.await_args.kwargs
    assert kwargs["channel_id"] == "DSTARTER"
    assert kwargs["thread_ts"] == "100.1"
    assert kwargs["automated"] is True
    assert "authorised for interactive" in kwargs["text"]


@pytest.mark.asyncio
async def test_silent_review_sends_nothing_when_agent_verdict_is_ok() -> None:
    settings = Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")
    agent = AsyncMock()
    agent.execute_once.return_value = SimpleNamespace(
        text="Всё проверено.\nEXPERIMENT_CONFIG_VERDICT: OK"
    )
    client = AsyncMock()
    payload = experiment_config_payload(_event())
    assert payload is not None

    notified = await ExperimentConfigResponder(settings, agent).review_and_publish(
        payload, client, timeout_seconds=300
    )

    assert notified is False
    client.conversations_open.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_silent_review_publishes_only_agent_result_when_issues_exist() -> None:
    settings = Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")
    agent = AsyncMock()
    agent.execute_once.return_value = SimpleNamespace(
        text=(
            "Проект: https://alice.mu.se/pages/123\n"
            "Админка: https://example.test/7890\n"
            "*Нужно исправить*\nWrong activation event.\n"
            "EXPERIMENT_CONFIG_VERDICT: ISSUES"
        )
    )
    client = AsyncMock()
    client.conversations_open.return_value = {"channel": {"id": "DSTARTER"}}
    client.chat_postMessage.return_value = {"ts": "200.1"}
    payload = experiment_config_payload(_event())
    assert payload is not None

    notified = await ExperimentConfigResponder(settings, agent).review_and_publish(
        payload, client, timeout_seconds=300
    )

    assert notified is True
    sent_text = client.chat_postMessage.await_args.kwargs["text"]
    assert sent_text.startswith(":wave: Привет, <@USTARTER>! Ты недавно запустил эксперимент")
    assert "<https://alice.mu.se/pages/123|«[UG Monetization] Test»>" in sent_text
    assert sent_text.endswith("*Нужно исправить*\nWrong activation event.")
    assert "Проект:" not in sent_text
    assert "Админка:" not in sent_text
    agent.attach_session.assert_awaited_once_with(
        "DSTARTER", "200.1", agent.execute_once.return_value
    )
