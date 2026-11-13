from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from sloperator.agents import HeadlessAgentRun
from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.config import Settings
from sloperator.experiment_finalizer import (
    CALC_API_COMMAND,
    FINALIZER_CALC_PRIORITY,
    FINALIZER_CALC_SOURCE,
    MATURITY_FORECAST_COMMENT_PREFIX,
    NO_OP_NOTIFICATION,
    QUEUE_FORECAST_RESULT_PREFIX,
    REVIEW_PROMPT,
    SWEEP_DONE_PREFIX,
    SWEEP_FAILED_PREFIX,
    SWEEP_RESULT_PREFIX,
    InvalidFinalizationNotification,
    start_prompt,
)
from sloperator.store import EventStore

NICOSIA = ZoneInfo("Asia/Nicosia")
# The sweep JQL quotes the summary as an exact phrase: summary ~ "\"Experiment results\"".
SUMMARY_JQL = 'summary ~ "\\"Experiment results\\""'
CRM_JQL_2026_11_13 = (
    'project = UMN AND assignee = "712020:e603f3a9-4b70-4ed8-866f-280460a661c5" '
    'AND summary ~ "\\"Experiment results\\"" AND status in (Backlog, "To Do") '
    'AND (duedate <= "2026-11-13" OR duedate is EMPTY) ORDER BY duedate ASC, key ASC'
)


@pytest.fixture(autouse=True)
def _no_live_confluence(monkeypatch) -> None:
    monkeypatch.delenv("CONFLUENCE_PERSONAL_TOKEN", raising=False)
    monkeypatch.delenv("CONFLUENCE_TOKEN", raising=False)


def _settings(**overrides) -> Settings:
    return Settings(slack_user_id="UOWNER", bot_token="test", app_token="test", **overrides)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _by_job_agent() -> SimpleNamespace:
    texts = {
        "crm-finalizer-reviewer": NO_OP_NOTIFICATION,
        "crm-post-analysis-reviewer": NO_OP_NOTIFICATION,
        "crm-results-overdue-sweep": "OVERDUE_SWEEP_DONE: 0",
    }

    async def by_job(prompt, *args, **kwargs):
        job = kwargs["job_name"]
        return HeadlessAgentRun("claude", "opus", job, texts[job])

    return SimpleNamespace(execute_once=AsyncMock(side_effect=by_job), attach_session=AsyncMock())


def _client() -> SimpleNamespace:
    return SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"channel": "C", "ts": "1"}),
        conversations_open=AsyncMock(return_value={"channel": {"id": "DOWNER"}}),
    )


def _fake_run_once(failures: dict[str, BaseException]):
    calls: list[str] = []

    async def fake(client, agent, settings, flow):
        calls.append(flow.reviewer_job)
        if flow.reviewer_job in failures:
            raise failures[flow.reviewer_job]
        return NO_OP_NOTIFICATION

    return fake, calls


class StopLoop(Exception):
    pass


class FakeSlots:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now
        self.messages: list[str] = []
        self.targets: list[dt.datetime] = []
        self.compute = None

    async def next_slot(self, compute, logger, message):
        if self.targets:
            raise StopLoop
        self.compute = compute
        self.messages.append(message)
        self.targets.append(compute(self.now))
        return self.targets[-1]


def _store(tmp_path):
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="test",
        app_token="test",
        agent_workspace=tmp_path,
        database_path=tmp_path / "state.sqlite3",
    )
    store = EventStore(settings.database_path)
    store.initialize()
    return settings, store


def _run_status(store: EventStore, run_id: str) -> str:
    with sqlite3.connect(store.path) as connection:
        return connection.execute(
            "SELECT status FROM scheduled_agent_runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]


# --- Cases ------------------------------------------------------------------------------------


def test_c1_first_read_selection_rules_and_start_prompt() -> None:
    from sloperator.crm_finalizer import CRM_SELECTION_RULES, crm_first_read_flow

    start = _flat(crm_first_read_flow(_settings()).start_prompt)
    rules = _flat(CRM_SELECTION_RULES)
    for text in (rules, start):
        for required in (
            "`ug_experiment_calculator.get_crm_exps_list(config=cfg)`", "`provider_id = 1`",
            "`[UG][CRM]`", "`Experiment results`", "/remotelink", "`Iteration 1`",
            "every send's attribution window has closed",
            "No pending-trials gate applies to the first read",
            "within the last calendar month", "oldest",
        ):
            assert required in text, required
        for forbidden in (
            "get_ugm_exps_list", "pending-trials gate of rule 8 still applies",
            "at least 7 complete days",
        ):
            assert forbidden not in text, forbidden
    for required in (
        CALC_API_COMMAND, str(FINALIZER_CALC_PRIORITY), FINALIZER_CALC_SOURCE,
        "FINALIZATION_STARTED", NO_OP_NOTIFICATION, "Experiment finalisation failed:",
        QUEUE_FORECAST_RESULT_PREFIX,
    ):
        assert required in start, required


def test_c2_first_read_preparation_and_review_prompts() -> None:
    from sloperator.crm_finalizer import crm_first_read_flow

    flow = crm_first_read_flow(_settings())
    prep = _flat(flow.preparation_prompt)
    for required in (
        "After Split is not generated in the first read",
        "`ug_experiment_calculator.get_experiment_confluence_report_code(<exp_id>)`",
        "`TBU`", "oldest first", "FINALIZATION_PREPARED:", "Iteration 1",
    ):
        assert required in prep, required
    assert "get_crm_after_split_post_analysis_code" not in prep
    review = flow.review_prompt
    assert review != REVIEW_PROMPT
    assert "one prepared CRM send unit first read" in review
    assert "no After Split table was added" in _flat(review)
    assert "181" in review and "In Review" in review
    assert "Results calculated and published." in review
    assert review.count("{prepared_result}") == 1
    rest = review.replace("{prepared_result}", "")
    assert "{" not in rest and "}" not in rest
    review.format(prepared_result="X")


def test_c3_post_analysis_rules_and_start_prompt() -> None:
    from sloperator.crm_finalizer import CRM_POST_ANALYSIS_RULES, crm_post_analysis_flow

    start = _flat(crm_post_analysis_flow(_settings()).start_prompt)
    rules = _flat(CRM_POST_ANALYSIS_RULES)
    for text in (rules, start):
        for required in (
            "`In Review` or `Done`", "at least 7 complete days", "`UG_CRM`", "`CRM purchases`",
            "fresh calculation", "within the last calendar month", "oldest",
        ):
            assert required in text, required
    for required in (
        CALC_API_COMMAND,
        "Do not change the Results task status, assignee or any field in this pass.",
        "FINALIZATION_STARTED:", NO_OP_NOTIFICATION, "Experiment finalisation failed:",
    ):
        assert required in start, required


def test_c4_post_analysis_preparation_and_review_prompts() -> None:
    from sloperator.crm_finalizer import crm_post_analysis_flow

    flow = crm_post_analysis_flow(_settings())
    prep = _flat(flow.preparation_prompt)
    for required in (
        "`ug_experiment_calculator.get_crm_after_split_post_analysis_code(<exp_id>)`",
        "after its Insights", "`Post-analysis summary`", "Change nothing else on the page",
        "FINALIZATION_PREPARED:",
    ):
        assert required in prep, required
    review = flow.review_prompt
    flat_review = _flat(review)
    for required in (
        "Do not change the Results task status or any field",
        "exactly one publication comment", "After Split post-analysis published.",
    ):
        assert required in flat_review, required
    assert "181" not in review and "281" not in review
    assert review.count("{prepared_result}") == 1
    rest = review.replace("{prepared_result}", "")
    assert "{" not in rest and "}" not in rest
    review.format(prepared_result="X")


def test_c5_flows_and_run_names() -> None:
    from sloperator.crm_finalizer import (
        CRM_JOB_NAME,
        CRM_RUN_JOB_NAMES,
        CRM_SWEEP_JOB_NAME,
        crm_first_read_flow,
        crm_post_analysis_flow,
    )

    first = crm_first_read_flow(_settings())
    assert (first.reviewer_job, first.preparer_job) == (
        "crm-finalizer-reviewer", "crm-finalizer-preparer"
    )
    assert first.success_heading == "Results calculated and published."
    assert first.publish_no_op is False and first.recover_missing_page is True
    second = crm_post_analysis_flow(_settings())
    assert (second.reviewer_job, second.preparer_job) == (
        "crm-post-analysis-reviewer", "crm-post-analysis-preparer"
    )
    assert second.success_heading == "After Split post-analysis published."
    assert second.publish_no_op is False and second.recover_missing_page is False
    assert CRM_RUN_JOB_NAMES == (
        "crm-finalizer-preparer", "crm-finalizer-reviewer",
        "crm-post-analysis-preparer", "crm-post-analysis-reviewer",
    )
    assert (CRM_JOB_NAME, CRM_SWEEP_JOB_NAME) == ("crm-finalizer", "crm-results-overdue-sweep")


def test_c6_forecast_and_sweep_policies_use_the_crm_schedule() -> None:
    from sloperator.crm_finalizer import (
        CRM_OVERDUE_SWEEP_POLICY,
        CRM_UPCOMING_QUEUE_FORECAST_POLICY,
        crm_first_read_flow,
        render_crm_prompt,
    )

    settings = _settings(crm_finalizer_hour=14)
    assert "<CRM_RUN_TIME>" in CRM_UPCOMING_QUEUE_FORECAST_POLICY
    assert "<CRM_RUN_TIME>" in CRM_OVERDUE_SWEEP_POLICY
    forecast = render_crm_prompt(CRM_UPCOMING_QUEUE_FORECAST_POLICY, settings)
    rendered = (
        forecast,
        render_crm_prompt(CRM_OVERDUE_SWEEP_POLICY, settings),
        crm_first_read_flow(settings).start_prompt,
    )
    for text in rendered:
        assert "14:00 Asia/Nicosia" in text
        assert "<CRM_RUN_TIME>" not in text and "12:00 Asia/Nicosia" not in text
    assert MATURITY_FORECAST_COMMENT_PREFIX in forecast and QUEUE_FORECAST_RESULT_PREFIX in forecast


def test_c7_crm_sweep_jql_and_prompt() -> None:
    from sloperator.crm_finalizer import crm_overdue_sweep_jql, crm_overdue_sweep_prompt

    jql = crm_overdue_sweep_jql(dt.date(2026, 11, 13))
    assert jql == CRM_JQL_2026_11_13
    assert "Итоги" not in jql
    prompt = crm_overdue_sweep_prompt(dt.date(2026, 11, 13), _settings())
    for required in (
        jql, "2026-11-13", AUTOMATED_RESPONSE_STYLE, "14:00 Asia/Nicosia",
        SWEEP_RESULT_PREFIX, SWEEP_DONE_PREFIX, SWEEP_FAILED_PREFIX,
    ):
        assert required in prompt, required


async def test_c8_run_executes_both_passes_and_sweep_silently() -> None:
    from sloperator.crm_finalizer import CrmRunOutcome, run_crm_once

    agent, client = _by_job_agent(), _client()

    outcome = await run_crm_once(client, agent, _settings())

    assert outcome == CrmRunOutcome(
        first_read=NO_OP_NOTIFICATION, post_analysis=NO_OP_NOTIFICATION,
        sweep="OVERDUE_SWEEP_DONE: 0",
    )
    calls = agent.execute_once.await_args_list
    assert [call.kwargs["job_name"] for call in calls] == [
        "crm-finalizer-reviewer", "crm-post-analysis-reviewer", "crm-results-overdue-sweep",
    ]
    # The sweep's scope is its JQL; the shared Russian comment markers of today's sweep rules
    # ("Итоги пока не взяты в работу:", "Итоги ждут завершения эксперимента:") stay in the policy.
    sweep_jql = calls[2].args[0].rsplit("JQL:\n", 1)[1].strip()
    assert SUMMARY_JQL in sweep_jql and "Итоги" not in sweep_jql
    client.chat_postMessage.assert_not_awaited()


async def test_c9_pass_two_and_sweep_run_when_pass_one_fails(monkeypatch, caplog) -> None:
    from sloperator import experiment_finalizer
    from sloperator.crm_finalizer import run_crm_once

    fake, calls = _fake_run_once({"crm-finalizer-reviewer": RuntimeError("boom")})
    sweep = AsyncMock(return_value="OVERDUE_SWEEP_DONE: 0")
    monkeypatch.setattr(experiment_finalizer, "run_once", fake)
    monkeypatch.setattr(experiment_finalizer, "sweep_overdue_results", sweep)
    client = _client()
    caplog.set_level(logging.INFO, logger="sloperator.crm_finalizer")

    outcome = await run_crm_once(client, _by_job_agent(), _settings())

    assert outcome.first_read == "Experiment finalisation failed: boom"
    assert calls == ["crm-finalizer-reviewer", "crm-post-analysis-reviewer"]
    sweep.assert_awaited_once()
    assert sweep.await_args.kwargs["job_name"] == "crm-results-overdue-sweep"
    client.chat_postMessage.assert_not_awaited()
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


async def test_c10_daily_loop_uses_the_crm_hour(monkeypatch, caplog) -> None:
    from sloperator import crm_finalizer

    once = AsyncMock()
    monkeypatch.setattr(crm_finalizer, "run_crm_once", once)
    slots = FakeSlots(dt.datetime(2026, 11, 13, 10, 30, tzinfo=dt.UTC))
    settings = _settings(experiment_finalizer_hour=12, crm_finalizer_hour=14)
    caplog.set_level(logging.INFO, logger="sloperator.crm_finalizer")

    with pytest.raises(StopLoop):
        await crm_finalizer.run_crm_daily(
            _client(), _by_job_agent(), settings, enabled=lambda: True, slots=slots
        )

    assert slots.targets == [dt.datetime(2026, 11, 13, 14, 0, tzinfo=NICOSIA)]
    assert slots.messages == ["Next CRM finalizer run scheduled for %s"]
    once.assert_awaited_once()
    assert "Starting scheduled CRM finalizer run" in caplog.text
    assert "CRM finalizer run completed" in caplog.text


async def test_c11_startup_recovery_covers_crm_run_names_and_sweep(tmp_path) -> None:
    from sloperator.crm_finalizer import recover_interrupted_crm_runs

    settings, store = _store(tmp_path)
    store.create_scheduled_agent_run(
        "crm-sweep", "crm-results-overdue-sweep", "claude", "opus", "s1", "old"
    )
    store.create_scheduled_agent_run(
        "ugm-sweep", "results-overdue-sweep", "claude", "opus", "s2", "old"
    )
    agent = SimpleNamespace(
        resume_interrupted_headless=AsyncMock(return_value=[]),
        execute_once=AsyncMock(
            return_value=HeadlessAgentRun("claude", "opus", "new", "OVERDUE_SWEEP_DONE: 0")
        ),
    )

    await recover_interrupted_crm_runs(_client(), agent, store, settings)

    resumed = agent.resume_interrupted_headless.await_args_list
    assert [call.kwargs["job_name"] for call in resumed] == [
        "crm-finalizer-reviewer", "crm-finalizer-preparer",
        "crm-post-analysis-reviewer", "crm-post-analysis-preparer",
    ]
    assert all(
        call.args[0] == settings.experiment_finalizer_timeout_seconds for call in resumed
    )
    agent.execute_once.assert_awaited_once()
    assert agent.execute_once.await_args.kwargs["job_name"] == "crm-results-overdue-sweep"
    assert SUMMARY_JQL in agent.execute_once.await_args.args[0]
    left = [row["run_id"] for row in store.list_interrupted_scheduled_agent_runs()]
    assert left == ["ugm-sweep"]


def test_c12_failed_first_read_candidate_is_excluded_on_retry() -> None:
    from sloperator.crm_finalizer import crm_first_read_flow

    flow = crm_first_read_flow(_settings())
    failure = (
        "Experiment finalisation failed: CRM send 8180 (Bloomreach campaign "
        "6ad3b301871ac0c24b2616ca) has queued Results task UMN-13007 (To Do) under epic "
        "UMN-13000, but no project page can be found for it."
    )

    assert start_prompt([], flow) == flow.start_prompt
    retry = start_prompt([failure], flow)
    assert retry.startswith(flow.start_prompt)
    assert "experiment id (8180)" in retry
    assert "Jira task (UMN-13000, UMN-13007)" in retry


# --- Edge cases -------------------------------------------------------------------------------


def test_e1_render_keeps_braces_and_pads_hours() -> None:
    from sloperator.crm_finalizer import render_crm_prompt

    template = 'Run at <CRM_RUN_TIME>. ADF {"type": "doc"} and <CRM_RUN_TIME> again.'
    for hour, text in ((0, "00:00"), (23, "23:00")):
        settings = _settings(crm_finalizer_hour=hour, experiment_finalizer_timezone="Asia/Nicosia")
        assert render_crm_prompt(template, settings) == (
            f'Run at {text} Asia/Nicosia. ADF {{"type": "doc"}} and {text} Asia/Nicosia again.'
        )


def test_e2_render_uses_the_finalizer_timezone_and_leaves_plain_text() -> None:
    from sloperator.crm_finalizer import CRM_OVERDUE_SWEEP_POLICY, render_crm_prompt

    settings = _settings(crm_finalizer_hour=9, experiment_finalizer_timezone="Europe/London")
    assert "09:00 Europe/London" in render_crm_prompt(CRM_OVERDUE_SWEEP_POLICY, settings)
    assert render_crm_prompt("No time here.", settings) == "No time here."


async def test_e3_failed_post_analysis_does_not_stop_the_sweep(monkeypatch) -> None:
    from sloperator import experiment_finalizer
    from sloperator.crm_finalizer import run_crm_once

    fake, _ = _fake_run_once({"crm-post-analysis-reviewer": RuntimeError("pass2 down")})
    sweep = AsyncMock(return_value="OVERDUE_SWEEP_DONE: 0")
    monkeypatch.setattr(experiment_finalizer, "run_once", fake)
    monkeypatch.setattr(experiment_finalizer, "sweep_overdue_results", sweep)

    outcome = await run_crm_once(_client(), _by_job_agent(), _settings())

    assert outcome.post_analysis == "Experiment finalisation failed: pass2 down"
    assert outcome.first_read == NO_OP_NOTIFICATION
    sweep.assert_awaited_once()


async def test_e4_sweep_failure_is_reported_not_raised(monkeypatch, caplog) -> None:
    from sloperator import experiment_finalizer
    from sloperator.crm_finalizer import run_crm_once

    fake, _ = _fake_run_once({})
    monkeypatch.setattr(experiment_finalizer, "run_once", fake)
    monkeypatch.setattr(
        experiment_finalizer, "sweep_overdue_results",
        AsyncMock(side_effect=RuntimeError("jira 401")),
    )
    caplog.set_level(logging.INFO, logger="sloperator.crm_finalizer")

    outcome = await run_crm_once(_client(), _by_job_agent(), _settings())

    assert outcome.sweep == f"{SWEEP_FAILED_PREFIX} jira 401"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_e5_cancellation_propagates_and_stops_the_run(monkeypatch) -> None:
    from sloperator import experiment_finalizer
    from sloperator.crm_finalizer import run_crm_once

    fake, calls = _fake_run_once({"crm-finalizer-reviewer": asyncio.CancelledError()})
    sweep = AsyncMock()
    monkeypatch.setattr(experiment_finalizer, "run_once", fake)
    monkeypatch.setattr(experiment_finalizer, "sweep_overdue_results", sweep)

    with pytest.raises(asyncio.CancelledError):
        await run_crm_once(_client(), _by_job_agent(), _settings())

    assert calls == ["crm-finalizer-reviewer"]
    sweep.assert_not_awaited()


async def test_e6_disabled_from_admin_skips_the_run(monkeypatch, caplog) -> None:
    from sloperator import crm_finalizer

    once = AsyncMock()
    monkeypatch.setattr(crm_finalizer, "run_crm_once", once)
    slots = FakeSlots(dt.datetime(2026, 11, 13, 10, 30, tzinfo=dt.UTC))
    caplog.set_level(logging.INFO, logger="sloperator.crm_finalizer")

    with pytest.raises(StopLoop):
        await crm_finalizer.run_crm_daily(
            _client(), _by_job_agent(), _settings(), enabled=lambda: False, slots=slots
        )

    once.assert_not_awaited()
    assert "Scheduled CRM finalizer run disabled from admin" in caplog.text


async def test_e7_reached_slot_and_weekend_move_to_monday(monkeypatch) -> None:
    from sloperator import crm_finalizer

    monkeypatch.setattr(crm_finalizer, "run_crm_once", AsyncMock())
    monday = dt.datetime(2026, 11, 16, 14, 0, tzinfo=NICOSIA)
    for now in (
        dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.UTC),
    ):
        slots = FakeSlots(now)
        with pytest.raises(StopLoop):
            await crm_finalizer.run_crm_daily(
                _client(), _by_job_agent(), _settings(), slots=slots
            )
        assert slots.targets == [monday]


async def test_e8_invalid_recovered_post_analysis_is_failed_and_reported(
    tmp_path, monkeypatch
) -> None:
    from sloperator import experiment_finalizer
    from sloperator.crm_finalizer import recover_interrupted_crm_runs

    settings, store = _store(tmp_path)
    store.create_scheduled_agent_run(
        "pa-run", "crm-post-analysis-reviewer", "claude", "opus", "s1", "prompt"
    )
    notification = (
        "[Project](https://alice.example/project) — experiment "
        "[8180](https://www.ultimate-guitar.com/components/ab/experiment/view?id=8180), "
        "Iteration 1. Results calculated and published."
    )
    recovered = HeadlessAgentRun("claude", "opus", "s1", notification, run_id="pa-run")

    async def resume(timeout_seconds, *, job_name, accept_result):
        return [recovered] if job_name == "crm-post-analysis-reviewer" else []

    monkeypatch.setattr(
        experiment_finalizer, "publish_run",
        AsyncMock(side_effect=InvalidFinalizationNotification("bad")),
    )
    rerun = AsyncMock(return_value=None)
    monkeypatch.setattr(experiment_finalizer, "rerun_interrupted_overdue_sweep", rerun)
    agent = SimpleNamespace(resume_interrupted_headless=AsyncMock(side_effect=resume))
    client = _client()

    await recover_interrupted_crm_runs(client, agent, store, settings)

    assert _run_status(store, "pa-run") == "failed"
    client.conversations_open.assert_awaited_once_with(users="UOWNER")
    client.chat_postMessage.assert_awaited_once()
    assert client.chat_postMessage.await_args.kwargs["markdown_text"] == (
        "<@UOWNER> ⚠️ CRM finalizer остановлен: восстановленный запуск не прошёл проверку "
        "или вернул нефинальный результат. Сервис продолжает работать; задачу нужно "
        "запустить повторно."
    )
    assert agent.resume_interrupted_headless.await_count == 4
    rerun.assert_awaited_once()


async def test_e9_recovered_crm_no_op_is_logged_not_posted(tmp_path, monkeypatch, caplog) -> None:
    """R15: a CRM reviewer no-op recovered at startup is silent like a live CRM no-op."""
    from sloperator import experiment_finalizer
    from sloperator.crm_finalizer import recover_interrupted_crm_runs

    settings, store = _store(tmp_path)
    store.create_scheduled_agent_run(
        "noop-run", "crm-finalizer-reviewer", "claude", "opus", "s1", "prompt"
    )
    recovered = HeadlessAgentRun("claude", "opus", "s1", NO_OP_NOTIFICATION, run_id="noop-run")

    async def resume(timeout_seconds, *, job_name, accept_result):
        return [recovered] if job_name == "crm-finalizer-reviewer" else []

    publish = AsyncMock()
    monkeypatch.setattr(experiment_finalizer, "publish_run", publish)
    monkeypatch.setattr(
        experiment_finalizer, "rerun_interrupted_overdue_sweep", AsyncMock(return_value=None)
    )
    agent = SimpleNamespace(resume_interrupted_headless=AsyncMock(side_effect=resume))
    client = _client()
    caplog.set_level(logging.INFO, logger="sloperator.crm_finalizer")

    await recover_interrupted_crm_runs(client, agent, store, settings)

    publish.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()
    assert _run_status(store, "noop-run") == "completed"
    assert "no eligible CRM unit" in caplog.text


async def test_e10_recovery_passes_the_crm_flow_and_heading_on(tmp_path, monkeypatch) -> None:
    from sloperator import experiment_finalizer
    from sloperator.crm_finalizer import crm_post_analysis_flow, recover_interrupted_crm_runs

    settings, store = _store(tmp_path)
    flow = crm_post_analysis_flow(settings)
    page = "https://alice.mu.se/pages/viewpage.action?pageId=838613487"
    done_start = f"FINALIZATION_STARTED: 8180 | {page} | Iteration 1 | UMN-13007"
    new_start = f"FINALIZATION_STARTED: 8181 | {page} | Iteration 1 | UMN-13008"
    published = (
        "[Project](https://alice.example/project) — experiment "
        "[8180](https://www.ultimate-guitar.com/components/ab/experiment/view?id=8180), "
        "Iteration 1. After Split post-analysis published."
    )
    prepared = f"FINALIZATION_PREPARED: 8181 | {page} | Iteration 1"
    for run_id, job in (
        ("done", "crm-post-analysis-reviewer"), ("new", "crm-post-analysis-reviewer"),
        ("pub", "crm-post-analysis-reviewer"), ("prep", "crm-post-analysis-preparer"),
    ):
        store.create_scheduled_agent_run(run_id, job, "claude", "opus", run_id, "prompt")
    # The preparation of `done_start` already exists: its prompt carries the start line.
    store.create_scheduled_agent_run(
        "old-prep", "crm-post-analysis-preparer", "claude", "opus", "x", "start:\n" + done_start
    )
    store.finish_scheduled_agent_run("old-prep", status="completed")
    runs = {
        "crm-post-analysis-reviewer": [
            HeadlessAgentRun("claude", "opus", "done", done_start, run_id="done"),
            HeadlessAgentRun("claude", "opus", "new", new_start, run_id="new"),
            HeadlessAgentRun("claude", "opus", "pub", published, run_id="pub"),
        ],
        "crm-post-analysis-preparer": [
            HeadlessAgentRun("claude", "opus", "prep", prepared, run_id="prep"),
        ],
    }
    accepts = {}

    async def resume(timeout_seconds, *, job_name, accept_result):
        accepts[job_name] = accept_result
        return runs.get(job_name, [])

    prepare, publish, review = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(experiment_finalizer, "run_preparation_from_started_run", prepare)
    monkeypatch.setattr(experiment_finalizer, "publish_run", publish)
    monkeypatch.setattr(experiment_finalizer, "run_review", review)
    monkeypatch.setattr(
        experiment_finalizer, "rerun_interrupted_overdue_sweep", AsyncMock(return_value=None)
    )
    agent = SimpleNamespace(resume_interrupted_headless=AsyncMock(side_effect=resume))

    await recover_interrupted_crm_runs(_client(), agent, store, settings)

    accept = accepts["crm-post-analysis-reviewer"]
    assert accept(published) and not accept(published.replace(
        "After Split post-analysis published.", "Results calculated and published."
    ))
    prepare.assert_awaited_once()
    assert prepare.await_args.args[3].text == new_start
    assert prepare.await_args.kwargs["flow"] == flow
    publish.assert_awaited_once()
    assert publish.await_args.kwargs["flow"] == flow
    review.assert_awaited_once()
    assert review.await_args.kwargs["flow"] == flow
    assert [_run_status(store, run_id) for run_id in ("done", "new", "pub", "prep")] == [
        "completed"
    ] * 4
