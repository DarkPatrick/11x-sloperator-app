# ruff: noqa: RUF001
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
    assert "calculator auto-substitution does not count" in prompt
    assert "always report that as a" in prompt
    assert "segments: {'Total': {'pro_rights': 'all'}}" in prompt


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
            "**Нужно исправить**\nДата окончания не проставлена.\nEXPERIMENT_CONFIG_VERDICT: ISSUES"
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
        "<https://www.ultimate-guitar.com/components/ab/experiment/view?id=7890|id 7890>" in intro
    )


def test_project_links_move_to_intro_and_identity_lines_leave_body() -> None:
    issue_experiments, projects, body = extract_project_links_and_clean_body(
        "**[UG Monetization] Test** (id 7890)\n"
        "Проект: https://alice.mu.se/pages/123\n"
        "Админка: https://example.test/7890\n\n"
        "**Нужно исправить**\nWrong event.",
        [{"id": 7890, "name": "[UG Monetization] Test"}],
    )

    assert issue_experiments == [{"id": 7890, "name": "[UG Monetization] Test"}]
    assert projects == ["https://alice.mu.se/pages/123"]
    assert body == "**Нужно исправить**\nWrong event."


def test_project_links_are_accepted_inside_experiment_headings() -> None:
    experiments = [
        {"id": 7898, "name": "[UG Monetization] iOS paywall"},
        {"id": 7874, "name": "[UG Monetization] top propensity"},
    ]

    issue_experiments, projects, body = extract_project_links_and_clean_body(
        "**[UG Monetization] iOS paywall** — "
        "[проект](https://alice.mu.se/pages/7898) · "
        "[админка](https://www.ultimate-guitar.com/components/ab/experiment/view?id=7898)\n"
        "**[UG Monetization] top propensity** — "
        "<https://alice.mu.se/pages/7874|проект> · "
        "<https://www.ultimate-guitar.com/components/ab/experiment/view?id=7874|админка>\n\n"
        "**Нужно исправить**\nWrong segments.",
        experiments,
    )

    assert issue_experiments == experiments
    assert projects == [
        "https://alice.mu.se/pages/7898",
        "https://alice.mu.se/pages/7874",
    ]
    assert body == "**Нужно исправить**\nWrong segments."


def test_issue_for_one_of_multiple_experiments_does_not_require_unrelated_link() -> None:
    experiments = [
        {"id": 7943, "name": "UG App: reduction in advertising volume (4 iteration)"},
        {"id": 7916, "name": "UG iOS: flo-paywall for top segment"},
    ]

    issue_experiments, projects, body = extract_project_links_and_clean_body(
        "**UG iOS: flo-paywall for top segment** — "
        "[проект](https://alice.mu.se/pages/7916) · "
        "[админка](https://www.ultimate-guitar.com/components/ab/experiment/view?id=7916)\n\n"
        "**Нужно исправить**\nWrong branches.",
        experiments,
    )

    assert issue_experiments == [experiments[1]]
    assert projects == ["https://alice.mu.se/pages/7916"]
    assert body == "**Нужно исправить**\nWrong branches."


@pytest.mark.asyncio
async def test_legacy_trigger_splits_experiments_into_independent_reviews() -> None:
    settings = Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")
    agent = AsyncMock()
    client = AsyncMock()
    responder = ExperimentConfigResponder(settings, agent)
    responder.review_and_publish = AsyncMock()  # type: ignore[method-assign]
    event = _event()
    payload = event["metadata"]["event_payload"]
    payload["experiments"].append({"id": 7916, "name": "Second experiment"})

    await responder.handle(event, client)

    calls = responder.review_and_publish.await_args_list
    assert [call.args[0]["experiments"] for call in calls] == [
        [experiment] for experiment in payload["experiments"]
    ]
    agent.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_silent_review_sends_nothing_when_agent_verdict_is_ok() -> None:
    settings = Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")
    agent = AsyncMock()
    agent.communication = None
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
    agent.communication = None
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


@pytest.mark.parametrize(
    "links",
    [
        "[Проект](https://alice.mu.se/pages/123) · "
        "[Админка](https://www.ultimate-guitar.com/components/ab/experiment/view?id=7952)",
        "<https://alice.mu.se/pages/123|Проект> · "
        "<https://www.ultimate-guitar.com/components/ab/experiment/view?id=7952|Админка>",
        "**Проект:** https://alice.mu.se/pages/123",
    ],
)
def test_separate_project_links_from_failed_7952_report(links: str) -> None:
    experiment = {"id": 7952, "name": "UG iOS: trial ineligible – consent sheet"}
    code = "```\nproject: https://alice.mu.se/pages/123, segments: {'Total': {}}\n```"
    linked, projects, body = extract_project_links_and_clean_body(
        f"**{experiment['name']}** (7952)\n{links}\n\nНужно исправить\n{code}",
        [experiment],
    )
    assert linked == [experiment]
    assert projects == ["https://alice.mu.se/pages/123"]
    assert body == f"Нужно исправить\n{code}"


def test_does_not_silently_drop_second_experiment_without_link() -> None:
    with pytest.raises(ValueError, match="must link every experiment"):
        extract_project_links_and_clean_body(
            "First [Проект](https://alice.mu.se/pages/1)\nSecond\nНужно исправить\nFix both",
            [{"id": 1, "name": "First"}, {"id": 2, "name": "Second"}],
        )


def test_separate_links_are_associated_with_each_experiment() -> None:
    experiments = [{"id": 1, "name": "First"}, {"id": 2, "name": "Second"}]
    linked, projects, _ = extract_project_links_and_clean_body(
        "First\n[Проект](https://alice.mu.se/pages/1)\nНужно исправить\nFix first\n"
        "Second\n[Проект](https://alice.mu.se/pages/2)\nНужно исправить\nFix second",
        experiments,
    )
    assert linked == experiments
    assert projects == ["https://alice.mu.se/pages/1", "https://alice.mu.se/pages/2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change_code", [False, True])
async def test_scheduled_notification_uses_communication_and_preserves_code(
    change_code: bool,
) -> None:
    settings = Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")
    agent = AsyncMock()
    code = "```\nsegments: {'Total': {'pro_rights': 'free'}}\n```"
    agent.execute_once.return_value = SimpleNamespace(
        text=f"[Проект](https://alice.mu.se/pages/123)\nНужно исправить\n{code}\n"
        "Детали в приложенном архиве.\nEXPERIMENT_CONFIG_VERDICT: ISSUES"
    )
    agent.communication.render.return_value = "Нужно исправить\n" + (
        code.replace("free", "all") if change_code else code
    )
    client = AsyncMock()
    client.conversations_open.return_value = {"channel": {"id": "DSTARTER"}}
    client.chat_postMessage.return_value = {"ts": "200.1"}
    payload = experiment_config_payload(_event())
    assert payload is not None
    responder = ExperimentConfigResponder(settings, agent)
    if change_code:
        with pytest.raises(ValueError, match="changed experiment configuration code"):
            await responder.review_and_publish(payload, client, timeout_seconds=300)
        client.chat_postMessage.assert_not_awaited()
        client.conversations_open.assert_not_awaited()
    else:
        assert await responder.review_and_publish(payload, client, timeout_seconds=300)
        agent.communication.render.assert_awaited_once()
        sent = client.chat_postMessage.await_args.kwargs["text"]
        assert code in sent
        assert "архив" not in sent
        assert "<https://alice.mu.se/pages/123|" in sent


@pytest.mark.asyncio
async def test_publication_failure_marks_persisted_run_failed() -> None:
    settings = Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")
    agent = AsyncMock()
    agent.store = Mock()
    agent.execute_once.return_value = SimpleNamespace(
        text="Нужно исправить\nMissing link\nEXPERIMENT_CONFIG_VERDICT: ISSUES",
        run_id="failed-run",
    )
    payload = experiment_config_payload(_event())
    assert payload is not None
    client = AsyncMock()
    with pytest.raises(ValueError, match="must link every experiment"):
        await ExperimentConfigResponder(settings, agent).review_and_publish(
            payload, client, timeout_seconds=300
        )
    agent.store.finish_scheduled_agent_run.assert_called_once_with(
        "failed-run",
        status="failed",
        last_error="Experiment config publication failed: agent response must link every "
        "experiment mentioned in the issue report",
    )
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_grouped_http_review_is_rejected_before_starting_agent() -> None:
    settings = Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")
    agent = AsyncMock()
    client = AsyncMock()
    payload = experiment_config_payload(_event())
    assert payload is not None
    payload["experiments"].append({"id": 7916, "name": "Another experiment"})
    with pytest.raises(ValueError, match="one experiment per review"):
        await ExperimentConfigResponder(settings, agent).review_and_publish(
            payload, client, timeout_seconds=300
        )
    agent.execute_once.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_starter_gets_separate_messages_and_provider_sessions() -> None:
    settings = Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")
    agent = AsyncMock()
    agent.communication = None
    client = AsyncMock()
    client.conversations_open.return_value = {"channel": {"id": "DSTARTER"}}
    client.chat_postMessage.side_effect = [{"ts": "200.1"}, {"ts": "200.2"}]
    experiments = [{"id": 7943, "name": "Advertising"}, {"id": 7916, "name": "Flo paywall"}]
    runs = [
        SimpleNamespace(
            text=f"Проект: https://alice.mu.se/pages/{experiment['id']}\n"
            f"Нужно исправить\nFix {experiment['id']}.\nEXPERIMENT_CONFIG_VERDICT: ISSUES",
            session_id=f"session-{experiment['id']}",
        )
        for experiment in experiments
    ]
    agent.execute_once.side_effect = runs
    responder = ExperimentConfigResponder(settings, agent)
    for experiment in experiments:
        assert await responder.review_and_publish(
            {"recipient_id": "USTARTER", "experiments": [experiment]},
            client,
            timeout_seconds=300,
        )
    for index, experiment in enumerate(experiments):
        other = experiments[1 - index]
        prompt = agent.execute_once.await_args_list[index].args[0]
        sent = client.chat_postMessage.await_args_list[index].kwargs["text"]
        assert experiment["name"] in prompt and other["name"] not in prompt
        assert experiment["name"] in sent and other["name"] not in sent
        assert str(experiment["id"]) in sent and str(other["id"]) not in sent
        attached = agent.attach_session.await_args_list[index].args
        assert attached == ("DSTARTER", f"200.{index + 1}", runs[index])
