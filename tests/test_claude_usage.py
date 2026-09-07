import pytest

from sloperator.claude_usage import ClaudeUsageError, parse_usage


def test_parse_usage_reports_remaining_percentages() -> None:
    usage = parse_usage(
        "Current session: 25% used · resets Sep 7, 5:40pm (UTC)\n"
        "Current week (all models): 12% used · resets Sep 9, 1pm (UTC)\n"
    )
    assert usage.session_remaining_percent == 75
    assert usage.week_remaining_percent == 88
    assert usage.session_reset_text == "Sep 7, 5:40pm (UTC)"


def test_parse_usage_fails_closed_on_missing_limits() -> None:
    with pytest.raises(ClaudeUsageError):
        parse_usage("Current session: unavailable")
