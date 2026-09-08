from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.claude_usage import ClaudeUsageError, parse_usage, usage_diagnostic
from sloperator.config import Settings
from sloperator.jira_task_automation import ClaudeUsageAlert


def test_parse_usage_reports_remaining_percentages() -> None:
    usage = parse_usage(
        "Current session: 25% used · resets Sep 7, 5:40pm (UTC)\n"
        "Current week (all models): 12% used · resets Sep 9, 1pm (UTC)\n"
    )
    assert usage.session_remaining_percent == 75
    assert usage.week_remaining_percent == 88
    assert usage.session_reset_text == "Sep 7, 5:40pm (UTC)"


def test_parse_usage_allows_blank_lines_between_limits_and_100_percent() -> None:
    usage = parse_usage(
        "Current session: 100% used · resets Sep 8, 1:19pm (UTC)\n\n"
        "Current week (all models): 100% used · resets Sep 9, 12:59pm (UTC)\n"
    )
    assert usage.session_remaining_percent == 0
    assert usage.week_remaining_percent == 0


def test_parse_usage_fails_closed_on_missing_limits() -> None:
    with pytest.raises(ClaudeUsageError):
        parse_usage("Current session: unavailable")


def test_parse_usage_accepts_unused_session_without_reset_time() -> None:
    usage = parse_usage(
        "Current session: 0% used\n"
        "Current week (all models): 23% used · resets Sep 9, 12:59pm (UTC)\n"
    )
    assert usage.session_remaining_percent == 100
    assert usage.week_remaining_percent == 77
    assert usage.session_reset_text == ""
    assert usage.week_reset_text == "Sep 9, 12:59pm (UTC)"


@pytest.mark.parametrize(
    "text",
    [
        "Current session: 0% used\n",
        "Current session: 0% used\nCurrent week (all models): 23% used\n",
        "Current session: unavailable\n"
        "Current week (all models): 23% used · resets Sep 9, 12:59pm (UTC)\n",
        "Current session: 101% used\n"
        "Current week (all models): 23% used · resets Sep 9, 12:59pm (UTC)\n",
    ],
)
def test_parse_usage_still_requires_valid_limits_and_weekly_reset(text: str) -> None:
    with pytest.raises(ClaudeUsageError):
        parse_usage(text)


def test_usage_diagnostic_keeps_only_quota_lines() -> None:
    diagnostic = usage_diagnostic(
        "private text\nCurrent session: 100% used · resets today\n"
        "Current week (all models): 22% used · resets tomorrow\nsecret text"
    )
    assert diagnostic == (
        "Current session: 100% used · resets today | "
        "Current week (all models): 22% used · resets tomorrow"
    )


async def test_claude_usage_alert_sends_one_owner_dm_per_hour() -> None:
    client = SimpleNamespace(
        conversations_open=AsyncMock(return_value={"channel": {"id": "D123"}}),
        chat_postMessage=AsyncMock(),
    )
    settings = Settings(slack_user_id="UOWNER", bot_token="xoxb-test", app_token="xapp-test")
    alert = ClaudeUsageAlert(client, settings)
    error = ClaudeUsageError(
        "Claude /usage output has no session and weekly limits",
        diagnostic="Current session: unavailable",
    )

    await alert(error)
    await alert(error)

    client.conversations_open.assert_awaited_once_with(users="UOWNER")
    client.chat_postMessage.assert_awaited_once()
    message = client.chat_postMessage.await_args.kwargs["markdown_text"]
    assert "Current session: unavailable" in message
    assert "xoxb-test" not in message
