from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call
from zoneinfo import ZoneInfo

import pytest

from sloperator.agents import HeadlessAgentRun
from sloperator.claude_usage import ClaudeUsage
from sloperator.config import Settings
from sloperator.daily_activity_digest import (
    CHANNEL_ID,
    DIGEST_PROMPT,
    FINALIZATION_FORECAST_JOB_NAME,
    FINALIZATION_FORECAST_PROMPT,
    DigestFacts,
    DigestItem,
    _activity_refs,
    _continuation_title,
    _experiment_config_ids,
    _general_queue,
    _merge_completed_session,
    _parse_finalization_forecast,
    _run_task_keys,
    _slack_session_item,
    build_digest,
    digest_quota_allows_launch,
    forecast_finalization,
    is_valid_agent_digest,
    next_run_at,
    render_fallback,
    resume_interrupted,
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


def test_friday_digest_continues_next_week() -> None:
    friday = dt.datetime(2026, 10, 2, 16, tzinfo=dt.UTC)
    thursday = friday - dt.timedelta(days=1)

    assert _continuation_title(friday) == "Продолжу на следующей неделе"
    assert _continuation_title(thursday) == "Продолжу завтра"


def test_digest_uses_80_percent_session_limit_and_ignores_weekly_limit() -> None:
    assert digest_quota_allows_launch(ClaudeUsage(79, 100, "", "Oct 2, 10:00PM"))
    assert not digest_quota_allows_launch(ClaudeUsage(80, 0, "", "Oct 2, 10:00PM"))


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


def test_finalization_forecast_is_one_linked_jira_task(tmp_path) -> None:
    item = _parse_finalization_forecast(
        'Internal checks complete.\nFINALIZATION_FORECAST: {"id":"7982",'
        '"task":"UMN-14000","title":"Итоги - Experiment 7982"}',
        settings(tmp_path),
    )

    assert item is not None
    assert item.task_key == "UMN-14000"
    assert item.jira_url.endswith("/browse/UMN-14000")
    assert "эксперимента 7982" in item.note

    with pytest.raises(ValueError, match="unambiguous"):
        _parse_finalization_forecast(
            'FINALIZATION_FORECAST: {"id":"7982","task":"UMN-14000","title":"First"}\n'
            'FINALIZATION_FORECAST: {"id":"7983","task":"UMN-14001","title":"Second"}',
            settings(tmp_path),
        )


@pytest.mark.asyncio
async def test_finalization_forecast_uses_next_scheduled_run(tmp_path) -> None:
    agent = AsyncMock()
    agent.execute_once.return_value = HeadlessAgentRun(
        "claude",
        "claude-opus-5-5",
        "forecast-session",
        'FINALIZATION_FORECAST: {"id":"7982","task":"UMN-14000",'
        '"title":"Итоги - Experiment 7982"}',
    )
    now = dt.datetime(2026, 9, 28, 16, 0, tzinfo=dt.UTC)

    item = await forecast_finalization(settings(tmp_path), agent, now=now)

    assert item is not None
    call = agent.execute_once.await_args
    assert FINALIZATION_FORECAST_PROMPT in call.args[0]
    assert "2026-09-29T12:00:00+03:00" in call.args[0]
    assert call.kwargs["job_name"] == FINALIZATION_FORECAST_JOB_NAME
    assert call.kwargs["workspace"] == settings(tmp_path).agent_workspace
    assert call.kwargs["max_interim_results"] == 0


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


def test_experiment_config_ids_come_from_authoritative_selection() -> None:
    run = {
        "channel_name": "experiment-config-check",
        "status": "completed",
        "messages": [
            {
                "text": (
                    "Experiments to review:\n"
                    "- id=8045; name=First\n"
                    "- id=8054; name=Second\n\n"
                    "For every experiment inspect the configuration. Example id=9999."
                )
            },
            {"text": "Validated experiment 8045 and experiment 8054."},
        ],
    }

    assert _experiment_config_ids(run) == ("8045", "8054")


def test_completed_slack_session_links_followup_as_one_analysis() -> None:
    shaped = _slack_session_item(
        {
            "channel_id": "C123",
            "channel_name": "ug-monetization-metrics-monitoring",
            "thread_ts": "1790580639.245759",
            "status": "idle",
            "turn_count": 2,
            "agent_name": "mobile-health/slack",
        }
    )

    assert shaped is not None
    bucket, item = shaped
    assert bucket == "completed"
    assert item.note == "выполнил разбор и дополнил его по запросу"  # noqa: RUF001
    assert item.jira_url == (
        "https://mu--se.slack.com/archives/C123/p1790580639245759"
        "?thread_ts=1790580639.245759&cid=C123"
    )


def test_running_slack_session_is_active() -> None:
    shaped = _slack_session_item(
        {
            "channel_id": "C456",
            "channel_name": "ug-analytics-monitoring",
            "thread_ts": "1790602046.066299",
            "status": "running",
            "turn_count": 0,
            "agent_name": "anomaly-alerts/slack",
        }
    )

    assert shaped is not None
    bucket, item = shaped
    assert bucket == "active"
    assert item.title == "Разбор аномалий в #ug-analytics-monitoring"
    assert item.note == "сейчас выполняю разбор"


def test_general_queue_excludes_tasks_owned_by_specialized_crons() -> None:
    tasks = [
        SimpleNamespace(key="UMN-1", summary="Собрать справочник тарифов"),
        SimpleNamespace(key="UMN-2", summary="Итоги - paywall experiment"),
        SimpleNamespace(key="UMN-3", summary="Расчет сверху и план тестирования"),
        SimpleNamespace(key="UMN-4", summary="Аналитика - checkout"),
    ]

    assert [task.key for task in _general_queue(tasks)] == ["UMN-1"]


def test_worker_and_slack_reviewer_result_merge_by_confluence_page() -> None:
    completed = [
        DigestItem(
            "UMN-13067",
            "Итоги эксперимента",
            "https://mu--se.atlassian.net/browse/UMN-13067",
            "подготовлен документ",
            "https://alice.mu.se/pages/viewpage.action?pageId=835327251",
        )
    ]
    session = DigestItem(
        "slack:C123:123.456",
        "Обновление страницы с итогами эксперимента",  # noqa: RUF001
        "https://mu--se.slack.com/archives/C123/p123456?thread_ts=123.456&cid=C123",
        "обновил страницу с итогами",  # noqa: RUF001
    )
    refs = _activity_refs(
        "<https://alice.mu.se/spaces/CRO/pages/835327251/experiment|Итоги>"
    )

    assert _merge_completed_session(completed, session, refs)
    assert len(completed) == 1
    assert completed[0].slack_urls == (session.jira_url,)
    rendered = render_fallback(DigestFacts(tuple(completed), ()))
    assert rendered.count("Итоги эксперимента") == 1
    assert "[результат в Slack](https://mu--se.slack.com/archives/C123/" in rendered


@pytest.mark.asyncio
async def test_low_quota_uses_fallback_without_agent(monkeypatch, tmp_path) -> None:
    current_facts = replace(facts(), quota_note="Лимита для полного запуска недостаточно.")
    monkeypatch.setattr(
        "sloperator.daily_activity_digest.collect_facts",
        AsyncMock(return_value=(current_facts, ClaudeUsage(80, 10, "", "Oct 2, 10:00PM"))),
    )
    agent = AsyncMock()

    message, used_agent = await build_digest(settings(tmp_path), AsyncMock(), agent)

    assert not used_agent
    assert "Лимита" in message
    agent.execute_once.assert_not_called()


@pytest.mark.asyncio
async def test_low_quota_creates_deferred_slack_agent_thread(monkeypatch, tmp_path) -> None:
    current_facts = replace(facts(), quota_note="temporary quota note")
    monkeypatch.setattr(
        "sloperator.daily_activity_digest.collect_facts",
        AsyncMock(return_value=(current_facts, ClaudeUsage(80, 10, "", "Oct 2, 10:00PM"))),
    )
    agent = AsyncMock()
    client = AsyncMock()
    client.chat_postMessage.return_value = {"channel": CHANNEL_ID, "ts": "100.1"}

    message, used_agent = await run_once(client, agent, settings(tmp_path), AsyncMock())

    assert message == "Собираю информацию..."
    assert not used_agent
    client.chat_postMessage.assert_awaited_once_with(
        channel=CHANNEL_ID,
        text="Собираю информацию...",
    )
    submit = agent.submit.await_args.kwargs
    assert submit["channel_id"] == CHANNEL_ID
    assert submit["message_ts"] == "100.1"
    assert submit["thread_ts"] == "100.1"
    assert submit["wait_for_quota_admission"] is True
    assert submit["quota_admission_session_used_limit"] == 80
    assert submit["quota_admission_check_weekly"] is False
    assert submit["agent_name"] == "daily-activity-digest/slack"
    assert "temporary quota note" not in submit["text"]


@pytest.mark.asyncio
async def test_agent_failure_uses_fallback(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "sloperator.daily_activity_digest.collect_facts",
        AsyncMock(return_value=(facts(), ClaudeUsage(0, 0, "", "Oct 2, 10:00PM"))),
    )
    monkeypatch.setattr(
        "sloperator.daily_activity_digest._add_finalization_forecast",
        AsyncMock(return_value=facts()),
    )
    agent = AsyncMock()
    agent.execute_once.side_effect = RuntimeError("provider unavailable")

    message, used_agent = await build_digest(settings(tmp_path), AsyncMock(), agent)

    assert not used_agent
    assert message == render_fallback(facts())


@pytest.mark.asyncio
async def test_successful_agent_digest_is_sent_to_channel(monkeypatch, tmp_path) -> None:
    draft = render_fallback(facts())
    monkeypatch.setattr(
        "sloperator.daily_activity_digest.collect_facts",
        AsyncMock(return_value=(facts(), ClaudeUsage(0, 0, "", "Oct 2, 10:00PM"))),
    )
    monkeypatch.setattr(
        "sloperator.daily_activity_digest._add_finalization_forecast",
        AsyncMock(return_value=facts()),
    )
    agent = AsyncMock()
    agent.execute_once.return_value = HeadlessAgentRun(
        "claude", "claude-opus-5-5", "session", draft
    )
    client = AsyncMock()

    message, used_agent = await run_once(client, agent, settings(tmp_path), AsyncMock())

    assert used_agent
    assert message == draft
    assert DIGEST_PROMPT in agent.execute_once.await_args.args[0]
    assert "AUTOMATED RESPONSE STYLE" in agent.execute_once.await_args.args[0]
    client.conversations_open.assert_not_awaited()
    assert client.chat_postMessage.await_args.kwargs["channel"] == CHANNEL_ID


@pytest.mark.asyncio
async def test_interrupted_forecast_resumes_and_finishes_digest(monkeypatch, tmp_path) -> None:
    current_facts = facts()
    monkeypatch.setattr(
        "sloperator.daily_activity_digest.collect_facts",
        AsyncMock(return_value=(current_facts, ClaudeUsage(99, 0, "", "Oct 5, 10:00PM"))),
    )
    formatted = render_fallback(current_facts)
    monkeypatch.setattr(
        "sloperator.daily_activity_digest._format_draft",
        AsyncMock(return_value=(formatted, True)),
    )
    recovered = HeadlessAgentRun(
        "claude",
        "claude-opus-5-5",
        "forecast-session",
        'FINALIZATION_FORECAST: {"id":"7982","task":"UMN-14000",'
        '"title":"Итоги - Experiment 7982"}',
        run_id="forecast-run",
        job_name=FINALIZATION_FORECAST_JOB_NAME,
    )
    agent = AsyncMock()
    agent.resume_interrupted_headless.side_effect = [[], [recovered]]
    client = AsyncMock()
    store = MagicMock()

    count = await resume_interrupted(client, agent, settings(tmp_path), store)

    assert count == 1
    assert agent.resume_interrupted_headless.await_count == 2
    client.chat_postMessage.assert_awaited_once()
    store.finish_scheduled_agent_run.assert_called_once_with(
        "forecast-run",
        status="completed",
        external_session_id="forecast-session",
        result_text=recovered.text,
    )


@pytest.mark.asyncio
async def test_interrupted_formatter_resumes_and_publishes_directly(tmp_path) -> None:
    message = render_fallback(facts())
    recovered = HeadlessAgentRun(
        "claude",
        "claude-opus-5-5",
        "digest-session",
        message,
        run_id="digest-run",
        job_name="daily-activity-digest",
    )
    agent = AsyncMock()
    agent.resume_interrupted_headless.return_value = [recovered]
    client = AsyncMock()
    store = MagicMock()
    store.list_interrupted_scheduled_agent_runs.return_value = [{
        "run_id": "forecast-run",
        "status": "recovered",
        "external_session_id": "forecast-session",
        "result_text": "FINALIZATION_FORECAST_NONE",
    }]

    count = await resume_interrupted(client, agent, settings(tmp_path), store)

    assert count == 1
    agent.resume_interrupted_headless.assert_awaited_once()
    client.chat_postMessage.assert_awaited_once_with(
        channel=CHANNEL_ID,
        markdown_text=message,
        unfurl_links=False,
        unfurl_media=False,
    )
    assert store.finish_scheduled_agent_run.call_args_list == [
        call(
            "digest-run",
            status="completed",
            external_session_id="digest-session",
            result_text=message,
        ),
        call(
            "forecast-run",
            status="completed",
            external_session_id="forecast-session",
            result_text="FINALIZATION_FORECAST_NONE",
        ),
    ]


def test_bot_channel_messages_returns_only_top_level_bot_posts(tmp_path) -> None:
    from sloperator.store import EventStore

    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.upsert_history_messages("C07A9FDQ14P", [
        {"ts": "1790683216.035599", "thread_ts": "1790683216.035599", "bot_id": "B1",
         "user": "UBOT", "text": "Experiment design automation failed: UMN-13525: x"},
        {"ts": "1790683719.754809", "thread_ts": "1790683216.035599", "bot_id": "B1",
         "user": "UBOT", "text": "reply"},
        {"ts": "1790683800.000001", "user": "UHUMAN", "text": "human"},
        {"ts": "1790500000.000001", "bot_id": "B1", "user": "UBOT", "text": "yesterday"},
    ])

    rows = store.bot_channel_messages(["C07A9FDQ14P"], 1790640000.0)

    assert [row["message_ts"] for row in rows] == ["1790683216.035599"]


async def test_published_automation_failure_is_reported_in_done_today(
    monkeypatch, tmp_path
) -> None:
    from sloperator.daily_activity_digest import collect_facts

    now = dt.datetime(2026, 9, 29, 16, 0, tzinfo=dt.UTC)
    posted = str(dt.datetime(2026, 9, 29, 12, 0, 16, tzinfo=dt.UTC).timestamp())
    store = SimpleNamespace(
        list_scheduled_agent_runs=lambda _limit: [],
        list_agent_sessions=lambda _limit: [],
        active_jira_task_agent_links=lambda: [],
        thread_messages=lambda *_args: [],
        bot_channel_messages=lambda channels, since: [
            {
                "channel_id": "C07A9FDQ14P",
                "message_ts": posted,
                "text": (
                    "Experiment design automation failed: UMN-13525: Jira epic UMN-13523 links "
                    "to multiple Confluence pages. Keep one project-page link on the epic."
                ),
            },
            {
                "channel_id": "C07A9FDQ14P",
                "message_ts": posted,
                "text": "Experiment finalisation failed: publish hook blocked the write",
            },
        ] if channels == ["C07A9FDQ14P"] and since <= float(posted) else [],
    )
    reader = SimpleNamespace(
        task_snapshot=AsyncMock(return_value=SimpleNamespace(
            summary="Расчет сверху и план тестирования - UG App: explore - Recommended course "
            "slot for Pro",
            status="In Progress",
        )),
        queued_tasks=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr("sloperator.daily_activity_digest.JiraTaskReader", lambda *_: reader)
    design = SimpleNamespace(task_key="UMN-13525")
    monkeypatch.setattr("sloperator.daily_activity_digest.select_design", AsyncMock(
        return_value=design))
    monkeypatch.setattr("sloperator.daily_activity_digest.select_analytics", AsyncMock(
        return_value=None))
    monkeypatch.setattr("sloperator.daily_activity_digest.read_usage", AsyncMock(
        return_value=ClaudeUsage(0, 0, "", "")))
    current = replace(settings(tmp_path), jira_username="user", jira_api_token="token")

    result, _usage = await collect_facts(current, store, now=now)

    items = {item.title: item for item in result.completed}
    finalizer_item = items.pop("Итоги эксперимента")
    (design_item,) = items.values()
    assert design_item.task_key == "UMN-13525"
    assert design_item.note == (
        "не удалось собрать дизайн эксперимента: нет однозначной ссылки на страницу проекта"
    )
    assert "Recommended course slot for Pro" in design_item.title
    assert not any(item.task_key == "UMN-13525" for item in result.pending)
    assert "[подробности в Slack](https://mu--se.slack.com/archives/C07A9FDQ14P/" in (
        finalizer_item.note
    )
    rendered = render_fallback(result)
    assert rendered.index("не удалось собрать дизайн") < rendered.index("*Продолжу")


def _stale_store(rows_by_job, posts):
    finished = []
    return SimpleNamespace(
        list_interrupted_scheduled_agent_runs=lambda job_name=None: rows_by_job.get(job_name, []),
        bot_channel_messages=lambda channels, since: [
            post for post in posts if float(post["message_ts"]) >= since
        ],
        finish_scheduled_agent_run=lambda run_id, **values: finished.append((run_id, values)),
    ), finished


async def test_previous_day_digest_run_is_abandoned_not_republished() -> None:
    from sloperator.daily_activity_digest import discard_stale_runs

    saturday = dt.datetime(2026, 10, 10, 13, 37, tzinfo=dt.UTC)
    store, finished = _stale_store(
        {"daily-activity-digest": [{
            "run_id": "friday", "job_name": "daily-activity-digest", "status": "running",
            "external_session_id": "s1", "result_text": None,
            "created_at": "2026-10-09 16:17:02",
        }]},
        [],
    )

    assert await discard_stale_runs(store, now=saturday) == 1
    assert finished[0][0] == "friday"
    assert finished[0][1]["status"] == "abandoned"


async def test_same_day_run_is_abandoned_once_its_digest_was_published() -> None:
    from sloperator.daily_activity_digest import discard_stale_runs

    evening = dt.datetime(2026, 10, 9, 17, 0, tzinfo=dt.UTC)
    started = dt.datetime(2026, 10, 9, 16, 17, 2, tzinfo=dt.UTC)
    store, finished = _stale_store(
        {"daily-activity-digest": [
            {"run_id": "timed-out", "job_name": "daily-activity-digest", "status": "running",
             "external_session_id": None, "result_text": None,
             "created_at": started.strftime("%Y-%m-%d %H:%M:%S")},
        ]},
        [{"message_ts": str(started.timestamp() + 180), "text": "_Сделано сегодня_\n• ..."}],
    )

    assert await discard_stale_runs(store, now=evening) == 1
    assert finished[0][1]["last_error"] == "digest already published for this run; not resumed"


async def test_same_day_unpublished_run_is_kept_for_resume() -> None:
    from sloperator.daily_activity_digest import discard_stale_runs

    evening = dt.datetime(2026, 10, 9, 17, 0, tzinfo=dt.UTC)
    store, finished = _stale_store(
        {FINALIZATION_FORECAST_JOB_NAME: [{
            "run_id": "forecast", "job_name": FINALIZATION_FORECAST_JOB_NAME,
            "status": "running", "external_session_id": None, "result_text": None,
            "created_at": "2026-10-09 16:00:01",
        }]},
        [{"message_ts": str(dt.datetime(2026, 10, 9, 10, tzinfo=dt.UTC).timestamp()),
          "text": "unrelated bot post"}],
    )

    assert await discard_stale_runs(store, now=evening) == 0
    assert finished == []


def _digest_store(**overrides):
    values = {
        "list_scheduled_agent_runs": lambda _limit: [],
        "list_agent_sessions": lambda _limit: [],
        "active_jira_task_agent_links": lambda: [],
        "thread_messages": lambda *_args: [],
        "bot_channel_messages": lambda *_args: [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_queues(monkeypatch, reader) -> None:
    monkeypatch.setattr("sloperator.daily_activity_digest.JiraTaskReader", lambda *_: reader)
    monkeypatch.setattr("sloperator.daily_activity_digest.select_design", AsyncMock(
        return_value=None))
    monkeypatch.setattr("sloperator.daily_activity_digest.select_analytics", AsyncMock(
        return_value=None))
    monkeypatch.setattr("sloperator.daily_activity_digest.read_usage", AsyncMock(
        return_value=ClaudeUsage(0, 0, "", "")))


async def test_no_eligible_finalizer_thread_is_not_reported_as_done(
    monkeypatch, tmp_path
) -> None:
    from sloperator.daily_activity_digest import collect_facts

    now = dt.datetime(2026, 10, 9, 16, 0, tzinfo=dt.UTC)
    session = {
        "channel_id": "C07A9FDQ14P", "channel_name": "ug-experiments",
        "thread_ts": "1791536598.030719", "agent_name": "experiment-finalizer/slack",
        "status": "idle", "turn_count": 1,
        "created_at": "2026-10-09 09:03:18", "updated_at": "2026-10-09 09:03:30",
    }
    store = _digest_store(
        list_agent_sessions=lambda _limit: [session],
        thread_messages=lambda *_args: [{
            "message_ts": "1791536598.030719",
            "text": "No eligible experiment was found for calculation today.",
        }],
    )
    reader = SimpleNamespace(queued_tasks=AsyncMock(return_value=[]))
    _patch_queues(monkeypatch, reader)
    current = replace(settings(tmp_path), jira_username="user", jira_api_token="token")

    result, _usage = await collect_facts(current, store, now=now)

    assert result.completed == ()
    assert "итогами" not in render_fallback(result)


async def test_task_waiting_for_author_gets_its_own_final_block(monkeypatch, tmp_path) -> None:
    from sloperator.daily_activity_digest import collect_facts
    from sloperator.jira_task_automation import ClarificationRequest

    now = dt.datetime(2026, 10, 9, 16, 0, tzinfo=dt.UTC)
    run = {
        "channel_name": "jira-task-reviewer", "status": "running",
        "updated_at": "2026-10-09 15:28:56",
        "messages": [{"text": "You own Jira task UMN-12379"}],
    }
    store = _digest_store(
        list_scheduled_agent_runs=lambda _limit: [run],
        active_jira_task_agent_links=lambda: [{"task_key": "UMN-12379"}],
    )
    reader = SimpleNamespace(
        task_snapshot=AsyncMock(return_value=SimpleNamespace(
            summary="Выводы - First session: Official tabs promo",
            status="В работе",  # noqa: RUF001
        )),
        queued_tasks=AsyncMock(return_value=[]),
        clarification_request=AsyncMock(
            return_value=ClarificationRequest("UMN-12379", "Elzira Badretdinova")
        ),
    )
    _patch_queues(monkeypatch, reader)
    current = replace(settings(tmp_path), jira_username="user", jira_api_token="token")

    result, _usage = await collect_facts(current, store, now=now)

    assert result.active == ()
    (item,) = result.waiting
    assert item.note == "задал вопрос в задаче, жду ответа от Elzira Badretdinova"
    rendered = render_fallback(result)
    assert rendered.rindex("*Жду разъяснений*") > rendered.index("*Продолжу")
    assert rendered.rstrip().endswith("жду ответа от Elzira Badretdinova.")
    assert is_valid_agent_digest(rendered, rendered)
    assert not is_valid_agent_digest(
        rendered.replace("Elzira Badretdinova", "автора"), rendered
    )


async def test_clarification_request_needs_latest_service_account_question() -> None:
    from sloperator.jira_task_automation import SERVICE_ACCOUNT_ID, JiraTaskReader

    def comment(account: str, text: str) -> dict:
        return {
            "author": {"accountId": account},
            "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": text}]}]},
        }

    reader = JiraTaskReader("https://jira", "user", "token")
    reader.assigned_by = AsyncMock(return_value="Elzira Badretdinova")  # type: ignore[method-assign]

    reader.recent_comments = AsyncMock(  # type: ignore[method-assign]
        return_value=[comment(SERVICE_ACCOUNT_ID, "Which result do you need here?")]
    )
    request = await reader.clarification_request("UMN-12389")
    assert request is not None and request.requested_from == "Elzira Badretdinova"

    reader.recent_comments = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            comment(SERVICE_ACCOUNT_ID, "Which result do you need here?"),
            comment("human", "Option 1, please."),
        ]
    )
    assert await reader.clarification_request("UMN-12389") is None

    reader.recent_comments = AsyncMock(  # type: ignore[method-assign]
        return_value=[comment(SERVICE_ACCOUNT_ID, "Done: results are on the page.")]
    )
    assert await reader.clarification_request("UMN-12389") is None
