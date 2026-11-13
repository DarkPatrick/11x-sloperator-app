import datetime as dt

from sloperator.claude_usage import ClaudeUsage
from sloperator.jira_task_automation import (
    confluence_destination,
    is_reserved_experiment_task,
    latest_external_confluence_activity,
    latest_external_confluence_comment,
    latest_external_jira_comment,
    reviewer_prompt,
    should_handle_confluence_comment,
    should_handle_jira_comment,
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


def test_quota_keeps_only_small_reserve_in_last_day_before_reset() -> None:
    now = dt.datetime(2026, 9, 9, 9, 0, tzinfo=dt.UTC)  # reset Sep 9, 1pm: 4 hours left
    assert weekly_quota_allows_launch(usage(0, 87), now=now)
    assert weekly_quota_allows_launch(usage(0, 94), now=now)
    assert not weekly_quota_allows_launch(usage(0, 95), now=now)
    assert not weekly_quota_allows_launch(usage(60, 50), now=now)


def test_quota_last_day_rule_needs_less_than_24_hours() -> None:
    reset_tomorrow = usage(0, 87, "Sep 10, 1pm (UTC)")
    # 23 hours before the reset: last-day reserve.
    now = dt.datetime(2026, 9, 9, 14, tzinfo=dt.UTC)
    assert weekly_quota_allows_launch(reset_tomorrow, now=now)
    # 25 hours before the reset: the daily reserve (14% per day) still applies.
    now = dt.datetime(2026, 9, 9, 12, tzinfo=dt.UTC)
    assert not weekly_quota_allows_launch(reset_tomorrow, now=now)


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
    assert "--reply-to-comment-id <COMMENT_ID>" in reviewer
    assert "real Jira mention" in reviewer


def test_confluence_destination_selects_parent_and_required_templates() -> None:
    release_parent, release_template = confluence_destination("Prepare product release notes")
    hypothesis_parent, hypothesis_template = confluence_destination("Проверить гипотезу оплаты")
    assert release_parent.endswith("3.+Product+Releases+um")
    assert release_template == "Product release"
    assert hypothesis_parent.endswith("2.+Hypothesis+um")
    assert hypothesis_template == "Hypotheses"


def test_latest_external_confluence_activity_ignores_service_account() -> None:
    comments = [
        {
            "author": "Pat Doe",
            "created": "2026-09-16T08:01:31.054Z",
            "updated": "2026-09-16T08:01:31.054Z",
        },
        {
            "author": "ug-ai-analyst",
            "created": "2026-09-16T08:05:00.000Z",
            "updated": "2026-09-16T08:05:00.000Z",
        },
    ]

    assert latest_external_confluence_activity(comments) == dt.datetime(
        2026, 9, 16, 8, 1, 31, 54000, tzinfo=dt.UTC
    )

    comments.append({
        "author": {"accountId": "712020:e603f3a9-4b70-4ed8-866f-280460a661c5"},
        "created": "2026-09-17T08:01:31.054Z",
    })
    assert latest_external_confluence_activity(comments) == dt.datetime(
        2026, 9, 16, 8, 1, 31, 54000, tzinfo=dt.UTC
    )


def test_latest_external_confluence_comment_uses_unprocessed_human_activity() -> None:
    comments = [
        {"id": "1", "author": "Pat Doe", "text": "Please fix this",
         "created": "2026-09-16T08:01:31Z"},
        {"id": "2", "author": "ug-ai-analyst", "text": "Fixed",
         "created": "2026-09-16T08:03:00Z"},
        {"id": "3", "author": "Sam Roe", "text": "@Pat please check",
         "created": "2026-09-16T08:04:00Z"},
        {"id": "4", "author": {"accountId": "712020:e603f3a9-4b70-4ed8-866f-280460a661c5"},
         "text": "Done", "created": "2026-09-16T08:05:00Z"},
    ]
    baseline = dt.datetime(2026, 9, 16, 8, 2, tzinfo=dt.UTC)
    assert latest_external_confluence_comment(comments, since=baseline) == comments[2]
    assert latest_external_confluence_comment(
        comments, since=dt.datetime(2026, 9, 16, 8, 5, tzinfo=dt.UTC)
    ) is None

def test_latest_external_jira_comment_selects_new_human_reply() -> None:
    comments = [
        {
            "id": "352762",
            "author": {"accountId": "human"},
            "created": "2026-09-17T11:43:59.369+0300",
            "parentId": 352750,
        },
        {
            "id": "352763",
            "author": {"accountId": "712020:e603f3a9-4b70-4ed8-866f-280460a661c5"},
            "created": "2026-09-17T11:48:11.739+0300",
        },
    ]

    assert latest_external_jira_comment(
        comments, since="2026-09-17T08:40:00+00:00"
    ) == comments[0]
    assert (
        latest_external_jira_comment(comments, since="2026-09-17T08:44:00+00:00")
        is None
    )


async def test_jira_comment_decision_requires_explicit_reply_verdict() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    task = SimpleNamespace(key="UMN-13435", summary="Analytics specification")
    comment = {"id": "354007", "body": "@Pat Doe проверь"}
    agent = SimpleNamespace(execute_once=AsyncMock(
        return_value=SimpleNamespace(text="IGNORE")
    ))

    assert not await should_handle_jira_comment(agent, task, comment, [comment])
    prompt = agent.execute_once.call_args.args[0]
    assert "AUTOMATED RESPONSE STYLE" in prompt
    assert "A comment explicitly addressed to another person" in prompt
    assert "354007" in prompt
    assert agent.execute_once.call_args.kwargs["job_name"] == "jira-comment-reply-decision"

    agent.execute_once.return_value = SimpleNamespace(text="REPLY")
    assert await should_handle_jira_comment(agent, task, comment, [comment])
    agent.execute_once.return_value = SimpleNamespace(text="REPLY with explanation")
    assert not await should_handle_jira_comment(agent, task, comment, [comment])


async def test_confluence_comment_decision_requires_explicit_reply_verdict() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    task = SimpleNamespace(key="UMN-13435", summary="Analytics specification")
    comment = {"id": "838611732", "author": "Sam Roe",
               "text": "@Pat Doe проверь"}
    agent = SimpleNamespace(execute_once=AsyncMock(
        return_value=SimpleNamespace(text="IGNORE")
    ))

    assert not await should_handle_confluence_comment(
        agent, task, "https://alice.mu.se/pages/viewpage.action?pageId=838602702",
        comment, [comment],
    )
    prompt = agent.execute_once.call_args.args[0]
    assert "AUTOMATED RESPONSE STYLE" in prompt
    assert "A comment addressed to another person" in prompt
    assert "838611732" in prompt
    assert agent.execute_once.call_args.kwargs["job_name"] == (
        "confluence-comment-reply-decision"
    )
    agent.execute_once.return_value = SimpleNamespace(text="REPLY")
    assert await should_handle_confluence_comment(
        agent, task, "https://alice.mu.se/pages/viewpage.action?pageId=838602702",
        comment, [comment],
    )
    agent.execute_once.return_value = SimpleNamespace(text="REPLY with explanation")
    assert not await should_handle_confluence_comment(
        agent, task, "https://alice.mu.se/pages/viewpage.action?pageId=838602702",
        comment, [comment],
    )


async def test_unrelated_confluence_comment_does_not_start_reviewer(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    import pytest

    from sloperator import jira_task_automation as automation

    link = {
        "task_key": "UMN-13435", "phase": "reviewer",
        "confluence_page_url": "https://alice.mu.se/pages/viewpage.action?pageId=838614955",
        "last_jira_updated_at": "2026-09-22T07:00:00+00:00",
        "last_confluence_activity_at": "2026-09-22T07:00:00+00:00",
    }
    task = SimpleNamespace(
        key="UMN-13435", summary="Analytics specification", description="",
        status="In Review", updated_at=dt.datetime(2026, 9, 22, 7, 0, tzinfo=dt.UTC),
    )
    comment = {
        "id": "838611732", "author": "Sam Roe",
        "text": "@Pat Doe проверь", "created": "2026-09-22T08:00:00Z",
    }
    reader = SimpleNamespace(
        task_snapshot=AsyncMock(return_value=task),
        recent_comments=AsyncMock(return_value=[]),
        was_returned_to_work=AsyncMock(return_value=False),
    )
    store = Mock()
    store.active_jira_task_agent_links.return_value = [link]
    store.jira_task_agent_link.return_value = link
    store.sync_completed_planner_tasks.return_value = 0
    agent = SimpleNamespace(store=store, execute_once=AsyncMock())
    settings = SimpleNamespace(jira_username="bot", jira_api_token="test",
                               jira_url="https://jira.invalid", agent_workspace=None)
    monkeypatch.setattr(automation, "JiraTaskReader", lambda *args: reader)
    monkeypatch.setattr(automation, "read_confluence_comments",
                        AsyncMock(return_value=[comment]))
    monkeypatch.setattr(automation, "should_handle_confluence_comment",
                        AsyncMock(return_value=False))
    monkeypatch.setattr(automation, "read_usage_or_alert", AsyncMock(return_value=object()))
    monkeypatch.setattr(automation, "weekly_quota_allows_launch", lambda *a, **kw: True)
    monkeypatch.setattr(automation.asyncio, "sleep",
                        AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await automation.poll_active_tasks(settings, agent)

    agent.execute_once.assert_not_called()
    store.upsert_jira_task_agent_link.assert_called_once_with(
        "UMN-13435", phase="reviewer",
        last_confluence_activity_at="2026-09-22T08:00:00+00:00",
    )


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

    task = SimpleNamespace(
        key="UMN-14000",
        summary="Geo discovery",
        description="Compare geos",
        updated_at=dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC),
    )
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


async def test_poll_closes_link_and_sessions_of_reassigned_task(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    import pytest

    from sloperator import jira_task_automation as automation

    link = {
        "task_key": "UMN-13632", "phase": "waiting",
        "worker_session_id": "worker-1", "reviewer_session_id": "owner-1",
        "last_jira_updated_at": "2026-10-12T07:00:00+00:00",
    }
    task = SimpleNamespace(
        key="UMN-13632", summary="Выводы", description="", status="В работе",  # noqa: RUF001
        updated_at=dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC),
        assignee_account_id="human-account",
    )
    reader = SimpleNamespace(
        task_snapshot=AsyncMock(return_value=task),
        recent_comments=AsyncMock(return_value=[]),
        was_returned_to_work=AsyncMock(return_value=False),
    )
    store = Mock()
    store.active_jira_task_agent_links.return_value = [link]
    store.jira_task_agent_link.return_value = link
    store.sync_completed_planner_tasks.return_value = 0
    agent = SimpleNamespace(store=store, execute_once=AsyncMock())
    settings = SimpleNamespace(jira_username="bot", jira_api_token="test",
                               jira_url="https://jira.invalid", agent_workspace=None)
    monkeypatch.setattr(automation, "JiraTaskReader", lambda *args: reader)
    monkeypatch.setattr(automation, "read_usage_or_alert", AsyncMock(return_value=object()))
    monkeypatch.setattr(automation, "weekly_quota_allows_launch", lambda *a, **kw: True)
    monkeypatch.setattr(automation.asyncio, "sleep",
                        AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await automation.poll_active_tasks(settings, agent)

    agent.execute_once.assert_not_called()
    reader.recent_comments.assert_not_called()
    (call,) = store.upsert_jira_task_agent_link.call_args_list
    assert call.args == ("UMN-13632",)
    assert call.kwargs["phase"] == "reassigned"
    assert call.kwargs["terminal_at"]
    store.close_agent_sessions_by_external_ids.assert_called_once_with(["worker-1", "owner-1"])


def test_is_reassigned_away() -> None:
    from types import SimpleNamespace

    from sloperator.jira_task_automation import SERVICE_ACCOUNT_ID, is_reassigned_away

    assert not is_reassigned_away(SimpleNamespace())
    assert not is_reassigned_away(SimpleNamespace(assignee_account_id=None))
    assert not is_reassigned_away(SimpleNamespace(assignee_account_id=SERVICE_ACCOUNT_ID))
    assert is_reassigned_away(SimpleNamespace(assignee_account_id=""))
    assert is_reassigned_away(SimpleNamespace(assignee_account_id="human"))


def test_confluence_destination_routes_mysql_docs_tasks() -> None:
    parent, template = confluence_destination("MySQL docs: update u_guitarcom.songs_base documentation")
    assert parent.endswith("/852018521/MySQL")
    assert template is not None and "mysql-table-docs" in template


def test_mysql_docs_task_is_not_reserved_by_a_table_name() -> None:
    assert not is_reserved_experiment_task("MySQL docs: u_guitarcom.survey_results")
    assert is_reserved_experiment_task("Experiment results for 8174")



def _resume_harness(
    monkeypatch, link: dict, replies: list[tuple[str, str]], *, assignee: str | None = None,
):
    """Poll one link with no Jira/Confluence activity; the store is a real dict."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from sloperator import jira_task_automation as automation

    task = SimpleNamespace(
        key=link["task_key"], summary="Monitoring", description="Long spec",
        status="In Progress",
        updated_at=dt.datetime(2026, 11, 10, 13, 50, tzinfo=dt.UTC),  # == cursor: no activity
        assignee_account_id=assignee or automation.SERVICE_ACCOUNT_ID,
    )
    reader = SimpleNamespace(
        task_snapshot=AsyncMock(return_value=task),
        recent_comments=AsyncMock(return_value=[]),
        was_returned_to_work=AsyncMock(return_value=False),
    )
    store = Mock()
    store.active_jira_task_agent_links.side_effect = lambda: [dict(link)]
    store.jira_task_agent_link.side_effect = lambda key: dict(link)
    store.sync_completed_planner_tasks.return_value = 0
    store.cleanup_jira_task_agent_links.return_value = 0
    store.upsert_jira_task_agent_link.side_effect = lambda key, **values: link.update(
        {k: v for k, v in values.items() if v is not None or k == "terminal_at"}
    )
    store.set_jira_task_progress.side_effect = lambda key, **values: link.update(values)
    agent = SimpleNamespace(store=store, execute_once=AsyncMock(side_effect=[
        SimpleNamespace(text=text, session_id=session) for text, session in replies
    ]))
    monkeypatch.setattr(automation, "JiraTaskReader", lambda *args: reader)
    monkeypatch.setattr(automation, "read_usage_or_alert", AsyncMock(return_value=object()))
    monkeypatch.setattr(automation, "weekly_quota_allows_launch", lambda *a, **kw: True)
    monkeypatch.setattr(automation, "abuse_precheck", AsyncMock(return_value=False))
    return automation, agent


async def _poll_once(monkeypatch, automation, agent) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    import pytest

    with monkeypatch.context() as patch:
        patch.setattr(automation.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await automation.poll_active_tasks(automation_settings(), agent)


def automation_settings():
    from types import SimpleNamespace

    return SimpleNamespace(jira_username="bot", jira_api_token="test",
                           jira_url="https://jira.invalid", agent_workspace=None)


def test_review_verdict_reads_only_the_final_line() -> None:
    from sloperator.jira_task_automation import review_verdict

    assert review_verdict("Done.\nTASK_DONE\n") == "TASK_DONE"
    assert review_verdict("redo X\n  TASK_REWORK  ") == "TASK_REWORK"
    assert review_verdict("TASK_DONE\nmore text") is None
    assert review_verdict("") is None


async def test_poll_resumes_unjudged_work_and_sends_it_back_for_rework(monkeypatch) -> None:
    # UMN-14338: the worker misread the task, the owner saw it, and nothing ever ran again.
    link = {
        "task_key": "UMN-14338", "phase": "review_pending",
        "worker_session_id": "worker-1", "reviewer_session_id": "owner-1",
        "last_jira_updated_at": "2026-11-10T13:50:00+00:00",
        "worker_handoff": "The task is empty", "rework_count": 0, "review_attempts": 0,
    }
    automation, agent = _resume_harness(monkeypatch, link, [
        ("The description is 49 KB; build the dashboard it asks for.\nTASK_REWORK", "owner-1"),
    ])
    await _poll_once(monkeypatch, automation, agent)

    (call,) = agent.execute_once.call_args_list
    assert call.kwargs["job_name"] == "jira-task-reviewer"
    assert "The task is empty" in call.args[0]
    assert "TASK_REWORK" in call.args[0]  # the verdict contract is in the prompt
    assert link["phase"] == "worker"
    assert link["rework_count"] == 1
    assert "49 KB" in link["rework_notes"]

    # Next poll, still no Jira activity: the worker runs with the notes, then the owner judges.
    agent.execute_once.side_effect = [
        type("R", (), {"text": "Dashboard built", "session_id": "worker-1"})(),
        type("R", (), {"text": "Published.\nTASK_DONE", "session_id": "owner-1"})(),
    ]
    agent.execute_once.reset_mock()
    await _poll_once(monkeypatch, automation, agent)
    worker_call, owner_call = agent.execute_once.call_args_list
    assert worker_call.kwargs["job_name"] == "jira-task-worker"
    assert worker_call.kwargs["existing_session_id"] == "worker-1"
    assert "49 KB" in worker_call.args[0]
    assert "Dashboard built" in owner_call.args[0]
    assert link["phase"] == "reviewer"
    assert link["worker_handoff"] is None
    assert link["rework_notes"] is None


async def test_poll_resumes_an_interrupted_worker_without_a_new_start_pass(monkeypatch) -> None:
    link = {
        "task_key": "UMN-14400", "phase": "worker", "reviewer_session_id": "owner-1",
        "last_jira_updated_at": "2026-11-10T13:50:00+00:00",
    }
    automation, agent = _resume_harness(monkeypatch, link, [
        ("Result", "worker-1"), ("Asked the author.\nTASK_WAITING", "owner-1"),
    ])
    await _poll_once(monkeypatch, automation, agent)
    assert [c.kwargs["job_name"] for c in agent.execute_once.call_args_list] == [
        "jira-task-worker", "jira-task-reviewer",
    ]
    assert link["phase"] == "waiting"


async def test_rework_is_capped(monkeypatch) -> None:
    link = {
        "task_key": "UMN-14401", "phase": "review_pending", "reviewer_session_id": "owner-1",
        "last_jira_updated_at": "2026-11-10T13:50:00+00:00",
        "worker_handoff": "Third try", "rework_count": 2,
    }
    automation, agent = _resume_harness(
        monkeypatch, link, [("Still wrong\nTASK_REWORK", "owner-1")],
    )
    await _poll_once(monkeypatch, automation, agent)
    assert link["phase"] == "waiting"


async def test_review_without_verdict_is_retried_then_parked(monkeypatch) -> None:
    link = {
        "task_key": "UMN-14402", "phase": "review_pending", "reviewer_session_id": "owner-1",
        "last_jira_updated_at": "2026-11-10T13:50:00+00:00",
        "worker_handoff": "Result", "review_attempts": 0,
    }
    automation, agent = _resume_harness(monkeypatch, link, [("no verdict", "owner-1")] * 3)
    for _ in range(2):
        await _poll_once(monkeypatch, automation, agent)
        assert link["phase"] == "review_pending"
    await _poll_once(monkeypatch, automation, agent)
    assert link["phase"] == "reviewer"
    assert link["review_attempts"] == 3


async def test_settled_owner_link_without_activity_stays_idle(monkeypatch) -> None:
    link = {
        "task_key": "UMN-14403", "phase": "reviewer", "reviewer_session_id": "owner-1",
        "last_jira_updated_at": "2026-11-10T13:50:00+00:00",
    }
    automation, agent = _resume_harness(monkeypatch, link, [])
    await _poll_once(monkeypatch, automation, agent)
    agent.execute_once.assert_not_called()


def test_store_progress_columns_round_trip(tmp_path) -> None:
    import pytest

    from sloperator.store import EventStore as Store

    store = Store(tmp_path / "s.sqlite3")
    store.initialize()
    store.upsert_jira_task_agent_link("UMN-1", phase="review_pending")
    store.set_jira_task_progress("UMN-1", worker_handoff="h", rework_count=1)
    assert store.jira_task_agent_link("UMN-1")["worker_handoff"] == "h"
    store.set_jira_task_progress("UMN-1", worker_handoff=None)
    row = store.jira_task_agent_link("UMN-1")
    assert row["worker_handoff"] is None and row["rework_count"] == 1
    with pytest.raises(ValueError):
        store.set_jira_task_progress("UMN-1", phase="x")
