import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sloperator import jira_task_automation as automation
from sloperator.planner_task_tracking import completed_task
from sloperator.store import EventStore


def record_review(store, run_id="review", status="completed", text=None):
    store.create_scheduled_agent_run(
        run_id,
        job_name="experiment-design-reviewer",
        provider="claude",
        model="opus",
        external_session_id="original-reviewer",
        prompt="Review exact task",
    )
    store.finish_scheduled_agent_run(
        run_id,
        status=status,
        result_text=text
        or "Reach & Impact ready — please check https://mu--se.atlassian.net/browse/UMN-123",
    )


def test_enrollment_is_durable_and_preserves_existing_ownership(tmp_path):
    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    record_review(store)
    record_review(store, "failed", "failed")
    record_review(store, "started", text="EXPERIMENT_TASK_STARTED: UMN-456 | UMN-1 | UMN-2")
    assert store.sync_completed_planner_tasks() == 1
    link = store.jira_task_agent_link("UMN-123")
    assert link["reviewer_session_id"] == "original-reviewer"
    assert link["last_jira_updated_at"]
    assert store.jira_task_agent_link("UMN-456") is None
    store.upsert_jira_task_agent_link("UMN-123", reviewer_session_id="continued")
    record_review(store, "another-review")
    assert store.sync_completed_planner_tasks() == 0
    assert store.jira_task_agent_link("UMN-123")["reviewer_session_id"] == "continued"
    store.upsert_jira_task_agent_link("UMN-123", terminal_at="2000-01-01T00:00:00+00:00")
    store.cleanup_jira_task_agent_links()
    assert store.sync_completed_planner_tasks() == 0
    assert store.active_jira_task_agent_links() == []


def test_finalizer_uses_verified_task_not_other_links():
    result = (
        "[Experiment](https://example.org/components/ab/experiment/view?id=12) "
        "— experiment 12. Results calculated and published."
    )
    prompt = (
        "Verified start:\n"
        "FINALIZATION_STARTED: 12 | https://example.org/page | Iteration 2 | UMN-123"
    )
    assert completed_task("experiment-finalizer-reviewer", result, prompt) == "UMN-123"
    assert completed_task("experiment-finalizer-reviewer", result, "") is None
    assert completed_task("experiment-finalizer-reviewer", "NO_OP: no work", prompt) is None


async def test_jira_activity_uses_actual_times_and_newest_comments(monkeypatch):
    response = SimpleNamespace(status=200, text=AsyncMock())
    session = MagicMock()
    session.get.return_value.__aenter__.return_value = response
    client = MagicMock()
    client.return_value.__aenter__.return_value = session
    monkeypatch.setattr(automation, "ClientSession", client)
    reader = automation.JiraTaskReader("https://jira.invalid", "bot", "token")
    response.text.return_value = json.dumps(
        {
            "changelog": {
                "histories": [
                    {
                        "created": "2026-09-14T11:00:00.000+0300",
                        "items": [
                            {
                                "field": "status",
                                "fromString": "In Review",
                                "toString": "In Progress",
                            }
                        ],
                    }
                ]
            }
        }
    )
    assert not await reader.was_returned_to_work("UMN-123", "2026-09-14T08:30:00+00:00")
    assert await reader.was_returned_to_work("UMN-123", "2026-09-14T07:30:00+00:00")
    response.text.return_value = json.dumps({"comments": [{"id": i} for i in range(20, 0, -1)]})
    assert [c["id"] for c in (await reader.recent_comments("UMN-123"))[-5:]] == list(range(16, 21))


def test_followup_keeps_description_text():
    assert (
        automation._adf_text(
            {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Correct this iteration"}],
                    }
                ],
            }
        )
        == "Correct this iteration"
    )


@pytest.mark.parametrize(
    "returned,human_comment,changed,expected",
    [
        (True, False, True, 3),
        (False, True, True, 1),
        (False, False, True, 0),
        (False, False, False, 0),
    ],
)
async def test_planner_activity_resumes_only_for_human_requests(
    tmp_path, monkeypatch, returned, human_comment, changed, expected
):
    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    record_review(store)
    store.sync_completed_planner_tasks()
    baseline = dt.datetime.fromisoformat(
        store.jira_task_agent_link("UMN-123")["last_jira_updated_at"]
    )
    task = SimpleNamespace(
        key="UMN-123",
        summary="Расчет сверху и план тестирования",
        description="Existing scope",
        status="In Progress",
        updated_at=baseline + dt.timedelta(seconds=int(changed)),
    )
    reader = SimpleNamespace(
        task_snapshot=AsyncMock(return_value=task),
        recent_comments=AsyncMock(return_value=[{
            "id": "new", "body": "Please correct",
            "author": {"accountId": "human" if human_comment else automation.SERVICE_ACCOUNT_ID},
            "created": (baseline + dt.timedelta(seconds=1)).isoformat(),
        }]),
        was_returned_to_work=AsyncMock(return_value=returned),
    )
    monkeypatch.setattr(automation, "JiraTaskReader", lambda *args: reader)
    monkeypatch.setattr(automation, "read_usage_or_alert", AsyncMock(return_value=object()))
    monkeypatch.setattr(automation, "weekly_quota_allows_launch", lambda *args, **kwargs: True)
    monkeypatch.setattr(automation, "abuse_precheck", AsyncMock(return_value=False))
    monkeypatch.setattr(automation.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    agent = SimpleNamespace(
        store=store,
        execute_once=AsyncMock(
            return_value=SimpleNamespace(
                text="TASK_READY",
                session_id="original-reviewer",
            )
        ),
    )
    settings = SimpleNamespace(
        jira_username="bot", jira_api_token="token", jira_url="https://jira.invalid"
    )
    with pytest.raises(asyncio.CancelledError):
        await automation.poll_active_tasks(settings, agent)
    assert agent.execute_once.call_count == expected
    if expected:
        calls = agent.execute_once.call_args_list
        assert calls[-1].kwargs["existing_session_id"] == "original-reviewer"
        if human_comment:
            assert "Please correct" in calls[-1].args[0]
        assert all("AUTOMATED RESPONSE STYLE" in call.args[0] for call in calls)
    assert automation.is_reserved_experiment_task(task.summary)


@pytest.mark.parametrize("author,seconds_after_baseline,expected", [
    ("human", -1, 0),
    (automation.SERVICE_ACCOUNT_ID, 1, 0),
    ("human", 1, 1),
])
async def test_confluence_followup_requires_new_human_comment(
    tmp_path, monkeypatch, author, seconds_after_baseline, expected
):
    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    record_review(store)
    store.sync_completed_planner_tasks()
    store.upsert_jira_task_agent_link(
        "UMN-123", phase="reviewer", confluence_page_url="https://confluence.invalid/page",
        confluence_page_version=10,
    )
    link = store.jira_task_agent_link("UMN-123")
    baseline = dt.datetime.fromisoformat(link["last_activity_at"]).replace(tzinfo=dt.UTC)
    task = SimpleNamespace(
        key="UMN-123", summary="Existing task", description="Existing scope",
        status="In Review", updated_at=dt.datetime.fromisoformat(link["last_jira_updated_at"]),
    )
    reader = SimpleNamespace(
        task_snapshot=AsyncMock(return_value=task),
        recent_comments=AsyncMock(return_value=[]),
        was_returned_to_work=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(automation, "JiraTaskReader", lambda *args: reader)
    monkeypatch.setattr(automation, "read_confluence_comments", AsyncMock(return_value=[{
        "author": {"accountId": author},
        "created": (baseline + dt.timedelta(seconds=seconds_after_baseline)).isoformat(),
    }]))
    monkeypatch.setattr(automation, "read_usage_or_alert", AsyncMock(return_value=object()))
    monkeypatch.setattr(automation, "weekly_quota_allows_launch", lambda *args, **kwargs: True)
    monkeypatch.setattr(automation, "abuse_precheck", AsyncMock(return_value=False))
    monkeypatch.setattr(automation.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    agent = SimpleNamespace(
        store=store,
        execute_once=AsyncMock(return_value=SimpleNamespace(text="Done", session_id="owner")),
    )
    settings = SimpleNamespace(
        jira_username="bot", jira_api_token="token", jira_url="https://jira.invalid",
        agent_workspace=tmp_path,
    )

    with pytest.raises(asyncio.CancelledError):
        await automation.poll_active_tasks(settings, agent)
    assert agent.execute_once.call_count == expected
