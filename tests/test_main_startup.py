"""Startup ordering of ``serve``: scheduler loops, recovery gate and the initial archive sync.

Case ids (C1, C2, C10, C12, E1-E3, E7, E8) refer to the code-pipeline test cases of the
recovery-gate change; tests named ``test_crm_*`` carry the case ids (C1-C11, E1-E7) of the CRM
finalizer loop wiring.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from sloperator import (
    automation_error_audit,
    crm_finalizer,
    daily_activity_digest,
    experiment_analytics_planner,
    experiment_design_planner,
    experiment_finalizer,
    main,
    mysql_docs_sync,
    scheduler_catchup,
    skill_docs_sync,
)
from sloperator.config import Settings
from sloperator.crm_finalizer import CRM_SWEEP_JOB_NAME, CrmRunOutcome
from sloperator.scheduled_jobs import EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME
from sloperator.store import EventStore

NIC = ZoneInfo("Asia/Nicosia")
LOOP_TASKS = {
    "daily-experiment-finalizer",
    "daily-crm-finalizer",
    "daily-experiment-design-planner",
    "daily-experiment-analytics-planner",
    "hourly-jira-task-automation",
    "ten-minute-jira-task-poll",
    "daily-automation-error-audit",
    "weekday-daily-activity-digest",
    "daily-skill-docs-sync",
    "weekly-mysql-docs-sync",
}
NEXT_LINES = (
    "Next experiment finalizer run scheduled for",
    "Next CRM finalizer run scheduled for",
    "Next experiment design run scheduled for",
    "Next experiment analytics run scheduled for",
    "Next automation error audit scheduled for",
    "Next daily activity digest scheduled for",
    "Next skill docs sync scheduled for",
    "Next MySQL docs sync scheduled for",
)
JIRA_CHECKS = (
    "Jira task automation skipped: Jira credentials are not configured",
    "Jira task polling skipped: Jira credentials are not configured",
)
GATE_LINE = "is waiting for the recovery of its interrupted run"
CRM_NEXT = "Next CRM finalizer run scheduled for"
UGM_NEXT = "Next experiment finalizer run scheduled for"


class Events(logging.Handler):
    """Log messages and stub markers in one ordered list."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.items: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.items.append(record.getMessage())

    def has(self, text: str) -> bool:
        return any(text in item for item in self.items)

    def count(self, text: str) -> int:
        return sum(text in item for item in self.items)

    def index(self, text: str) -> int:
        return next(i for i, item in enumerate(self.items) if text in item)


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.settings = Settings(
            "UOWNER", "xoxb-test", "xapp-test", database_path=tmp_path / "db.sqlite3"
        )
        self.store = EventStore(self.settings.database_path)
        self.store.initialize()
        self.chain = asyncio.Event()
        self.chain_error: BaseException | None = None
        self.archive = asyncio.Event()
        self.archive.set()
        self.now = dt.datetime(2026, 11, 11, 9, tzinfo=dt.UTC)
        self.events = Events()
        self._logger = logging.getLogger("sloperator")
        self._level = self._logger.level
        self._logger.setLevel(logging.INFO)
        self._logger.addHandler(self.events)
        self.bodies = {
            module.__name__: AsyncMock(return_value="done")
            for module in (
                automation_error_audit,
                daily_activity_digest,
                experiment_analytics_planner,
                experiment_design_planner,
                experiment_finalizer,
                mysql_docs_sync,
                skill_docs_sync,
            )
        }
        for name, body in self.bodies.items():
            monkeypatch.setattr(f"{name}.run_once", body)
        monkeypatch.setattr(experiment_finalizer, "sweep_overdue_results", AsyncMock())
        self.crm_body = AsyncMock(return_value=CrmRunOutcome("noop", "noop", "sweep"))
        monkeypatch.setattr(crm_finalizer, "run_crm_once", self.crm_body)
        self.app = SimpleNamespace(client=AsyncMock())
        self.orchestrators: list[object] = []
        self.crm_recovery = AsyncMock(
            side_effect=lambda *_a: self.events.items.append("crm-recovery")
        )
        self.ugm_sweep_recovery = AsyncMock(
            side_effect=lambda *_a, **_k: self.events.items.append("ugm-sweep-recovery")
        )
        monkeypatch.setattr(scheduler_catchup, "utcnow", lambda: self.now)
        monkeypatch.setattr(scheduler_catchup, "sleep_until", self._sleep_forever)
        monkeypatch.setattr(scheduler_catchup, "GATE_RECHECK_SECONDS", 0.01)
        self._stub_service(monkeypatch)

    async def _sleep_forever(self, _target: dt.datetime, _now: dt.datetime) -> None:
        await asyncio.Event().wait()

    def _stub_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = self

        class Orchestrator:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.resume_interrupted_headless = AsyncMock(return_value=[])
                self.close = AsyncMock()
                harness.orchestrators.append(self)

            def set_notification_client(self, _client: object) -> None:
                pass

            async def resume_interrupted(self, _client: object) -> None:
                harness.events.items.append("chain:started")
                await harness.chain.wait()
                if harness.chain_error is not None:
                    raise harness.chain_error

        class SocketHandler:
            def __init__(self, *_args: object) -> None:
                self.connect_async = AsyncMock()
                self.close_async = AsyncMock()

        class Runner:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.setup = AsyncMock()
                self.cleanup = AsyncMock()

        class Site:
            def __init__(self, *_args: object) -> None:
                self.start = AsyncMock()

        async def synchronize_archive(*_args: object) -> None:
            await harness.archive.wait()
            harness.events.items.append("archive:synchronized")

        async def forever(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        def noop(*_args: object, **_kwargs: object) -> None:
            return None

        for name, value in {
            "validate_agent_runtime": noop,
            "configure_operations": noop,
            "VpnManager": lambda _settings: SimpleNamespace(configured=False),
            "SlackCommunicationLayer": noop,
            "AgentOrchestrator": Orchestrator,
            "SubscriptionFlowResponder": noop,
            "PaymentLayerResponder": noop,
            "ExperimentConfigResponder": noop,
            "AirflowOwnerResponder": noop,
            "create_app": lambda *_a, **_k: harness.app,
            "AsyncSocketModeHandler": SocketHandler,
            "create_health_app": lambda: SimpleNamespace(router=Mock()),
            "create_admin_routes": noop,
            "ClaudeUsageAlert": noop,
            "rerun_interrupted_overdue_sweep": harness.ugm_sweep_recovery,
            "recover_interrupted_crm_runs": harness.crm_recovery,
            "resume_daily_activity_digest": AsyncMock(return_value=0),
            "synchronize_archive": synchronize_archive,
            "periodically_synchronize_archive": forever,
        }.items():
            monkeypatch.setattr(main, name, value)
        monkeypatch.setattr(main.web, "AppRunner", Runner)
        monkeypatch.setattr(main.web, "TCPSite", Site)

    def body(self, module: object) -> AsyncMock:
        return self.bodies[module.__name__]  # type: ignore[attr-defined]

    def record(self, job: str, slot: dt.datetime) -> None:
        self.store.record_scheduler_slot(job, slot.isoformat())

    def unfinished_run(self, run_id: str, job_name: str, status: str = "running") -> None:
        self.store.create_scheduled_agent_run(run_id, job_name, "claude", "opus", None, "prompt")
        if status != "running":
            self.store.finish_scheduled_agent_run(run_id, status=status)

    def finish(self, run_id: str, status: str = "completed") -> None:
        self.store.finish_scheduled_agent_run(run_id, status=status)

    def start(self) -> asyncio.Task[None]:
        return asyncio.create_task(main.serve(self.settings))

    @staticmethod
    def loop_tasks() -> dict[str, asyncio.Task[object]]:
        return {
            task.get_name(): task for task in asyncio.all_tasks() if task.get_name() in LOOP_TASKS
        }

    async def loops_started(self) -> None:
        await until(lambda: all(self.events.has(line) for line in (*NEXT_LINES, *JIRA_CHECKS)))

    def reset_events(self) -> None:
        self._logger.removeHandler(self.events)
        self.events = Events()
        self._logger.addHandler(self.events)

    def close(self) -> None:
        self._logger.removeHandler(self.events)
        self._logger.setLevel(self._level)

    async def stop(self, serve_task: asyncio.Task[None]) -> None:
        serve_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve_task


async def until(condition: Callable[[], bool], limit: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + limit
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


async def settle() -> None:
    """Give every runnable loop several gate re-checks worth of time."""
    await asyncio.sleep(0.1)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Harness]:
    created = Harness(monkeypatch, tmp_path)
    yield created
    created.close()


async def test_c1_loops_start_before_the_recovery_chain_returns(harness: Harness) -> None:
    serve_task = harness.start()
    await harness.loops_started()
    assert not harness.events.has("Sloperator started")
    assert len(harness.loop_tasks()) == len(LOOP_TASKS)

    harness.chain.set()
    await until(lambda: harness.events.has("Sloperator started"))
    events = harness.events
    assert events.count("Sloperator started") == 1
    assert (
        events.index("chain:started")
        < events.index("archive:synchronized")
        < events.index("Sloperator started")
    )
    await harness.stop(serve_task)


async def test_c2_shutdown_or_chain_error_cancels_every_loop(harness: Harness, caplog) -> None:
    serve_task = harness.start()
    await harness.loops_started()
    loops = harness.loop_tasks()
    await harness.stop(serve_task)
    assert loops and all(task.cancelled() for task in loops.values())

    harness.reset_events()
    harness.chain_error = RuntimeError("boom")
    serve_task = harness.start()
    await harness.loops_started()
    loops = harness.loop_tasks()
    harness.chain.set()
    with pytest.raises(RuntimeError, match="boom"):
        await serve_task
    assert len(loops) == len(LOOP_TASKS) and all(task.cancelled() for task in loops.values())
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_c10_gate_holds_only_the_job_with_an_interrupted_run(harness: Harness) -> None:
    harness.unfinished_run("audit-run", "automation-error-audit")
    harness.record("automation-error-audit", dt.datetime(2026, 11, 10, 14, tzinfo=NIC))
    harness.record("experiment-design-planner", dt.datetime(2026, 11, 10, 15, tzinfo=NIC))
    harness.now = dt.datetime(2026, 11, 11, 13, tzinfo=dt.UTC)
    serve_task = harness.start()
    await until(lambda: harness.body(experiment_design_planner).await_count == 1)
    await settle()
    assert harness.body(automation_error_audit).await_count == 0

    harness.finish("audit-run")
    await until(lambda: harness.body(automation_error_audit).await_count == 1)
    assert not harness.events.has("Sloperator started")  # chain still blocked
    assert harness.events.count(GATE_LINE) == 1
    assert "automation-error-audit" in harness.events.items[harness.events.index(GATE_LINE)]
    await harness.stop(serve_task)


async def test_c12_digest_waits_for_the_initial_archive_sync(harness: Harness) -> None:
    harness.chain.set()
    harness.archive.clear()
    harness.record("daily-activity-digest", dt.datetime(2026, 11, 10, 19, tzinfo=NIC))
    harness.now = dt.datetime(2026, 11, 11, 17, 5, tzinfo=dt.UTC)
    serve_task = harness.start()
    await until(lambda: harness.events.has("Catching up the missed daily-activity-digest"))
    await settle()
    assert harness.body(daily_activity_digest).await_count == 0

    harness.archive.set()
    await until(lambda: harness.body(daily_activity_digest).await_count == 1)
    assert harness.store.scheduler_slot("daily-activity-digest") == "2026-11-11T19:00:00+02:00"
    await harness.stop(serve_task)


@pytest.mark.parametrize("run_job", ["jira-task-worker", "jira-task-reviewer"])
async def test_e1_jira_loops_wait_for_their_interrupted_run(harness: Harness, run_job: str) -> None:
    harness.unfinished_run("jira-run", run_job, status="interrupted")
    serve_task = harness.start()
    await until(lambda: all(harness.events.has(line) for line in NEXT_LINES))
    await settle()
    assert not any(harness.events.has(check) for check in JIRA_CHECKS)

    harness.finish("jira-run", status="failed")
    await until(lambda: all(harness.events.has(check) for check in JIRA_CHECKS))
    await harness.stop(serve_task)


@pytest.mark.parametrize("pre_start_row", [True, False])
async def test_e2_gate_opens_at_chain_end_and_ignores_later_runs(
    harness: Harness, pre_start_row: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if pre_start_row:
        harness.unfinished_run("audit-before", "automation-error-audit")
    harness.record("automation-error-audit", dt.datetime(2026, 11, 10, 14, tzinfo=NIC))
    harness.now = dt.datetime(2026, 11, 11, 13, tzinfo=dt.UTC)
    snapshot = scheduler_catchup.RecoveryGate.snapshot

    async def snapshot_then_start_a_run(*args: object, **kwargs: object) -> object:
        gate = await snapshot(*args, **kwargs)
        # A run started after startup (by a loop) is never part of the gate.
        harness.unfinished_run("audit-after", "automation-error-audit")
        return gate

    monkeypatch.setattr(scheduler_catchup.RecoveryGate, "snapshot", snapshot_then_start_a_run)
    serve_task = harness.start()
    audit = harness.body(automation_error_audit)
    if pre_start_row:
        await until(lambda: harness.events.has(GATE_LINE))
        await settle()
        assert audit.await_count == 0
        harness.chain.set()
    await until(lambda: audit.await_count == 1)
    if not pre_start_row:
        assert not harness.chain.is_set() and not harness.events.has(GATE_LINE)
    await harness.stop(serve_task)


async def test_e3_finalizer_gate_covers_the_overdue_sweep(harness: Harness) -> None:
    harness.unfinished_run("sweep-run", experiment_finalizer.SWEEP_JOB_NAME)
    harness.record("experiment-finalizer", dt.datetime(2026, 11, 10, 12, tzinfo=NIC))
    harness.now = dt.datetime(2026, 11, 11, 10, 30, tzinfo=dt.UTC)
    serve_task = harness.start()
    await until(lambda: harness.events.has(GATE_LINE))
    await settle()
    assert harness.body(experiment_finalizer).await_count == 0

    harness.chain.set()
    await until(lambda: harness.body(experiment_finalizer).await_count == 1)
    await harness.stop(serve_task)


async def test_e7_disabled_settings_create_no_loop(harness: Harness) -> None:
    harness.settings = replace(
        harness.settings, experiment_design_enabled=False, jira_task_automation_enabled=False
    )
    serve_task = harness.start()
    started = [line for line in NEXT_LINES if line != "Next experiment design run scheduled for"]
    await until(lambda: all(harness.events.has(line) for line in started))
    await settle()
    tasks = harness.loop_tasks()
    assert set(tasks) == LOOP_TASKS - {
        "daily-experiment-design-planner",
        "hourly-jira-task-automation",
        "ten-minute-jira-task-poll",
    }
    assert not harness.events.has("Next experiment design run scheduled for")
    assert not harness.events.has(GATE_LINE)
    await harness.stop(serve_task)


async def test_e8_digest_slot_leaves_the_window_while_the_sync_blocks(harness: Harness) -> None:
    harness.chain.set()
    harness.archive.clear()
    harness.record("daily-activity-digest", dt.datetime(2026, 11, 10, 19, tzinfo=NIC))
    harness.now = dt.datetime(2026, 11, 11, 17, 5, tzinfo=dt.UTC)
    serve_task = harness.start()
    await until(lambda: harness.events.has("Catching up the missed daily-activity-digest"))
    harness.now = dt.datetime(2026, 11, 11, 22, 10, tzinfo=dt.UTC)
    harness.archive.set()
    await until(
        lambda: harness.events.has("Next daily activity digest scheduled for 2026-11-12T19:00")
    )
    assert harness.body(daily_activity_digest).await_count == 0
    await harness.stop(serve_task)


# CRM finalizer loop wiring (cases C1-C11, E1-E7 of the CRM loop module).

CRM_DISPLAY = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["crm-finalizer"].display_name
UGM_DISPLAY = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["experiment-finalizer"].display_name


def disable_cron(monkeypatch: pytest.MonkeyPatch, display_name: str) -> None:
    monkeypatch.setattr(
        main.AutomationControls,
        "disabled",
        lambda _self, kind, name: kind == "crons" and name == display_name,
    )


def both_slots_missed(harness: Harness) -> None:
    harness.record("experiment-finalizer", dt.datetime(2026, 11, 10, 12, tzinfo=NIC))
    harness.record("crm-finalizer", dt.datetime(2026, 11, 10, 14, tzinfo=NIC))
    harness.now = dt.datetime(2026, 11, 11, 12, 30, tzinfo=dt.UTC)


def last_line(harness: Harness, text: str) -> str:
    return [item for item in harness.events.items if text in item][-1]


async def test_crm_c1_loop_starts_with_default_schedule(harness: Harness) -> None:
    serve_task = harness.start()
    await harness.loops_started()
    tasks = harness.loop_tasks()
    assert {"daily-crm-finalizer", "daily-experiment-finalizer"} <= set(tasks)
    assert len(tasks) == len(LOOP_TASKS) == 10
    assert harness.events.has(f"{CRM_NEXT} 2026-11-11T14:00:00+02:00")
    await harness.stop(serve_task)


async def test_crm_c2_configured_hour_is_used(harness: Harness) -> None:
    harness.settings = replace(harness.settings, crm_finalizer_hour=16)
    serve_task = harness.start()
    await until(lambda: harness.events.has(CRM_NEXT) and harness.events.has(UGM_NEXT))
    assert harness.events.has(f"{CRM_NEXT} 2026-11-11T16:00:00+02:00")
    assert harness.events.has(f"{UGM_NEXT} 2026-11-11T12:00:00+02:00")
    await harness.stop(serve_task)


async def test_crm_c3_disabled_crm_setting_keeps_the_ugm_loop(harness: Harness) -> None:
    harness.settings = replace(harness.settings, crm_finalizer_enabled=False)
    serve_task = harness.start()
    started = [line for line in NEXT_LINES if line != CRM_NEXT]
    await until(lambda: all(harness.events.has(line) for line in started))
    await settle()
    assert set(harness.loop_tasks()) == LOOP_TASKS - {"daily-crm-finalizer"}
    assert not harness.events.has(CRM_NEXT)
    await harness.stop(serve_task)


async def test_crm_c4_disabled_ugm_setting_keeps_the_crm_loop(harness: Harness) -> None:
    harness.settings = replace(harness.settings, experiment_finalizer_enabled=False)
    serve_task = harness.start()
    await until(lambda: harness.events.has(CRM_NEXT))
    await settle()
    assert set(harness.loop_tasks()) == LOOP_TASKS - {"daily-experiment-finalizer"}
    assert not harness.events.has(UGM_NEXT)
    await harness.stop(serve_task)


async def test_crm_c5_missed_slot_runs_the_crm_body(harness: Harness) -> None:
    harness.chain.set()
    harness.record("crm-finalizer", dt.datetime(2026, 11, 10, 14, tzinfo=NIC))
    harness.now = dt.datetime(2026, 11, 11, 12, 30, tzinfo=dt.UTC)
    serve_task = harness.start()
    await until(lambda: harness.crm_body.await_count == 1)
    harness.crm_body.assert_awaited_once_with(
        harness.app.client, harness.orchestrators[0], harness.settings
    )
    assert harness.store.scheduler_slot("crm-finalizer") == "2026-11-11T14:00:00+02:00"
    await harness.stop(serve_task)


async def test_crm_c6_admin_toggle_skips_only_the_crm_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.chain.set()
    both_slots_missed(harness)
    disable_cron(monkeypatch, CRM_DISPLAY)
    serve_task = harness.start()
    await until(lambda: harness.body(experiment_finalizer).await_count == 1)
    await settle()
    assert harness.crm_body.await_count == 0
    assert harness.events.has("Scheduled CRM finalizer run disabled from admin")
    await harness.stop(serve_task)


async def test_crm_c7_ugm_admin_toggle_leaves_the_crm_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.chain.set()
    both_slots_missed(harness)
    disable_cron(monkeypatch, UGM_DISPLAY)
    serve_task = harness.start()
    await until(lambda: harness.crm_body.await_count == 1)
    await settle()
    assert harness.crm_body.await_count == 1
    assert harness.body(experiment_finalizer).await_count == 0
    await harness.stop(serve_task)


async def test_crm_c8_gate_holds_crm_loop_for_its_interrupted_run(harness: Harness) -> None:
    harness.unfinished_run("crm-run", "crm-post-analysis-reviewer")
    harness.record("crm-finalizer", dt.datetime(2026, 11, 10, 14, tzinfo=NIC))
    harness.now = dt.datetime(2026, 11, 11, 12, 30, tzinfo=dt.UTC)
    serve_task = harness.start()
    await until(lambda: harness.events.has(GATE_LINE))
    await settle()
    assert harness.crm_body.await_count == 0
    assert "crm-finalizer" in last_line(harness, GATE_LINE)

    harness.finish("crm-run")
    await until(lambda: harness.crm_body.await_count == 1)
    assert not harness.events.has("Sloperator started")  # chain still blocked
    await harness.stop(serve_task)


async def test_crm_c9_gate_covers_the_crm_sweep(harness: Harness) -> None:
    harness.unfinished_run("crm-run", CRM_SWEEP_JOB_NAME)
    harness.record("crm-finalizer", dt.datetime(2026, 11, 10, 14, tzinfo=NIC))
    harness.now = dt.datetime(2026, 11, 11, 12, 30, tzinfo=dt.UTC)
    serve_task = harness.start()
    await until(lambda: harness.events.has(GATE_LINE))
    await settle()
    assert "crm-finalizer" in last_line(harness, GATE_LINE)
    assert harness.crm_body.await_count == 0

    harness.chain.set()
    await until(lambda: harness.crm_body.await_count == 1)
    await harness.stop(serve_task)


async def test_crm_c10_startup_recovers_crm_runs_after_the_ugm_sweep(harness: Harness) -> None:
    harness.chain.set()
    serve_task = harness.start()
    await until(lambda: harness.events.has("Sloperator started"))
    harness.crm_recovery.assert_awaited_once()
    client, orchestrator, store, settings = harness.crm_recovery.await_args.args
    assert (client, orchestrator, settings) == (
        harness.app.client,
        harness.orchestrators[0],
        harness.settings,
    )
    assert isinstance(store, EventStore) and store.path == harness.settings.database_path.resolve()
    events = harness.events
    assert (
        events.index("ugm-sweep-recovery")
        < events.index("crm-recovery")
        < events.index("Sloperator started")
    )
    await harness.stop(serve_task)


async def test_crm_c11_crm_loop_is_cancelled_at_shutdown(harness: Harness, caplog) -> None:
    serve_task = harness.start()
    await harness.loops_started()
    loops = harness.loop_tasks()
    await harness.stop(serve_task)
    assert loops["daily-crm-finalizer"].cancelled()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize(
    "ugm_run_job", ["experiment-finalizer-reviewer", experiment_finalizer.SWEEP_JOB_NAME]
)
async def test_crm_e1_e2_ugm_gate_does_not_hold_the_crm_loop(
    harness: Harness, ugm_run_job: str
) -> None:
    # E1: the UGM reviewer run; E2: the UGM sweep (SWEEP_JOB_NAME) belongs to the UGM gate only.
    harness.unfinished_run("ugm-run", ugm_run_job)
    both_slots_missed(harness)
    serve_task = harness.start()
    await until(lambda: harness.crm_body.await_count == 1)
    await settle()
    assert harness.body(experiment_finalizer).await_count == 0
    assert harness.events.count(GATE_LINE) == 1
    assert "experiment-finalizer" in last_line(harness, GATE_LINE)
    await harness.stop(serve_task)


async def test_crm_e3_crm_recovery_error_cancels_every_loop(harness: Harness) -> None:
    harness.crm_recovery.side_effect = RuntimeError("crm boom")
    serve_task = harness.start()
    await harness.loops_started()
    loops = harness.loop_tasks()
    harness.chain.set()
    with pytest.raises(RuntimeError, match="crm boom"):
        # A dropped CRM recovery call leaves serve waiting forever; fail instead of hanging.
        await asyncio.wait_for(serve_task, timeout=3.0)
    assert set(loops) == LOOP_TASKS and all(task.cancelled() for task in loops.values())
    assert not harness.events.has("Sloperator started")


async def test_crm_e4_hour_zero_with_todays_slot_taken(harness: Harness) -> None:
    harness.settings = replace(harness.settings, crm_finalizer_hour=0)
    harness.record("crm-finalizer", dt.datetime(2026, 11, 11, 0, tzinfo=NIC))
    serve_task = harness.start()
    await until(lambda: harness.events.has(CRM_NEXT))
    await settle()
    assert harness.events.has(f"{CRM_NEXT} 2026-11-12T00:00:00+02:00")
    assert harness.crm_body.await_count == 0
    await harness.stop(serve_task)


async def test_crm_e5_hour_twenty_three(harness: Harness) -> None:
    harness.settings = replace(harness.settings, crm_finalizer_hour=23)
    harness.now = dt.datetime(2026, 11, 13, 20, tzinfo=dt.UTC)
    serve_task = harness.start()
    await until(lambda: harness.events.has(CRM_NEXT))
    assert harness.events.has(f"{CRM_NEXT} 2026-11-13T23:00:00+02:00")
    await harness.stop(serve_task)


async def test_crm_e6_missed_slot_is_caught_up_once(harness: Harness) -> None:
    harness.chain.set()
    harness.record("crm-finalizer", dt.datetime(2026, 11, 10, 14, tzinfo=NIC))
    harness.now = dt.datetime(2026, 11, 11, 12, 30, tzinfo=dt.UTC)
    serve_task = harness.start()
    await until(lambda: harness.crm_body.await_count == 1)
    await until(lambda: harness.events.has(f"{CRM_NEXT} 2026-11-12T14:00:00+02:00"))
    await settle()
    assert harness.crm_body.await_count == 1
    assert last_line(harness, CRM_NEXT).endswith("2026-11-12T14:00:00+02:00")
    await harness.stop(serve_task)


async def test_crm_e7_both_finalizers_disabled(harness: Harness) -> None:
    harness.settings = replace(
        harness.settings, experiment_finalizer_enabled=False, crm_finalizer_enabled=False
    )
    serve_task = harness.start()
    started = [line for line in NEXT_LINES if line not in (CRM_NEXT, UGM_NEXT)]
    await until(lambda: all(harness.events.has(line) for line in started))
    await settle()
    tasks = harness.loop_tasks()
    assert set(tasks) == LOOP_TASKS - {"daily-experiment-finalizer", "daily-crm-finalizer"}
    assert len(tasks) == 8
    assert not harness.events.has(GATE_LINE)
    assert not serve_task.done()
    await harness.stop(serve_task)
