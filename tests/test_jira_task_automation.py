import datetime as dt

from sloperator.claude_usage import ClaudeUsage
from sloperator.jira_task_automation import (
    confluence_destination,
    reviewer_prompt,
    weekly_quota_allows_launch,
    worker_prompt,
)


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
    assert "Jira is read-only for you" in worker
    assert "customfield_10312" in reviewer
    assert "Product release" in worker
    assert "Hypotheses" in worker
    assert "103614364" in worker
    assert "UMN-14000" in worker
    assert "AUTOMATED RESPONSE STYLE" in reviewer
    assert "duedate" in reviewer
    assert "transition with ID 181" in reviewer
    assert "Confluence page" in reviewer


def test_confluence_destination_selects_parent_and_required_templates() -> None:
    release_parent, release_template = confluence_destination("Prepare product release notes")
    hypothesis_parent, hypothesis_template = confluence_destination("Проверить гипотезу оплаты")
    assert release_parent.endswith("3.+Product+Releases+um")
    assert release_template == "Product release"
    assert hypothesis_parent.endswith("2.+Hypothesis+um")
    assert hypothesis_template == "Hypotheses"


async def test_reviewer_start_requires_explicit_verified_success() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from sloperator.jira_task_automation import start_task_with_reviewer

    for reply, ready in [("TASK_READY", True), ("TASK_WAITING", False),
                         ("TASK_START_FAILED", False), ("", False),
                         ("Could not verify TASK_READY", False)]:
        agent = SimpleNamespace(
            execute_once=AsyncMock(return_value=SimpleNamespace(text=reply, session_id="owner")),
            store=Mock(),
        )
        assert await start_task_with_reviewer(agent, "UMN-14000", {"reviewer_session_id": "owner"}) is ready
        call = agent.execute_once.call_args
        assert call.kwargs["existing_session_id"] == "owner"
        assert call.kwargs["job_name"] == "jira-task-reviewer"
        assert "AUTOMATED RESPONSE STYLE" in call.args[0]
        agent.store.upsert_jira_task_agent_link.assert_called_once_with(
            "UMN-14000", reviewer_session_id="owner", phase="worker" if ready else "waiting",
        )


async def test_hourly_starts_reviewer_before_worker_and_resumes_owner(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    import pytest

    from sloperator import jira_task_automation as automation

    task = SimpleNamespace(key="UMN-14000", summary="Geo discovery", description="Compare geos")
    reader = SimpleNamespace(queued_tasks=AsyncMock(return_value=[task]), recent_comments=AsyncMock(return_value=[]))
    monkeypatch.setattr(automation, "JiraTaskReader", lambda *args: reader)
    monkeypatch.setattr(automation, "read_usage_or_alert", AsyncMock(return_value=object()))
    monkeypatch.setattr(automation, "weekly_quota_allows_launch", lambda *args, **kwargs: True)
    monkeypatch.setattr(automation, "abuse_precheck", AsyncMock(return_value=False))
    monkeypatch.setattr(automation.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    link = {}
    store = Mock()
    store.jira_task_agent_link.side_effect = lambda key: dict(link) or None
    store.upsert_jira_task_agent_link.side_effect = lambda key, **values: link.update(values)
    agent = SimpleNamespace(store=store, execute_once=AsyncMock(side_effect=[
        SimpleNamespace(text="TASK_READY", session_id="owner"),
        SimpleNamespace(text="Private result", session_id="worker"),
        SimpleNamespace(text="Published", session_id="owner"),
    ]))
    settings = SimpleNamespace(jira_username="bot", jira_api_token="test", jira_url="https://jira.invalid")
    with pytest.raises(asyncio.CancelledError):
        await automation.run_hourly(settings, agent)
    calls = agent.execute_once.call_args_list
    assert [call.kwargs["job_name"] for call in calls] == [
        "jira-task-reviewer", "jira-task-worker", "jira-task-reviewer",
    ]
    assert calls[2].kwargs["existing_session_id"] == "owner"
    assert "Private result" in calls[2].args[0]
