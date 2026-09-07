import datetime as dt

from sloperator.claude_usage import ClaudeUsage
from sloperator.jira_task_automation import reviewer_prompt, weekly_quota_allows_launch, worker_prompt


def usage(session: int, week: int, reset: str = "Sep 9, 1pm (UTC)") -> ClaudeUsage:
    return ClaudeUsage(session, week, "Sep 7, 5:40pm (UTC)", reset)


def test_quota_requires_more_than_half_session_headroom() -> None:
    assert not weekly_quota_allows_launch(usage(50, 90), now=dt.datetime(2026, 9, 7, tzinfo=dt.UTC))


def test_quota_requires_daily_weekly_headroom() -> None:
    now = dt.datetime(2026, 9, 7, tzinfo=dt.UTC)
    assert not weekly_quota_allows_launch(usage(10, 72), now=now)
    assert weekly_quota_allows_launch(usage(10, 71), now=now)


def test_quota_allows_large_weekly_reserve() -> None:
    assert weekly_quota_allows_launch(
        usage(10, 9), now=dt.datetime(2026, 9, 7, tzinfo=dt.UTC)
    )


def test_worker_and_reviewer_prompts_are_task_scoped() -> None:
    worker = worker_prompt("UMN-14000")
    reviewer = reviewer_prompt("UMN-14000")
    assert "AUTOMATED RESPONSE STYLE" in worker
    assert "customfield_10312" in worker
    assert "Product release" in worker
    assert "Hypotheses" in worker
    assert "103614364" in worker
    assert "UMN-14000" in worker
    assert "AUTOMATED RESPONSE STYLE" in reviewer
    assert "duedate" in reviewer
    assert "transition with ID 181" in reviewer
    assert "Confluence page" in reviewer
