"""Startup ordering of ``serve``: scheduler loops, recovery gate and the initial archive sync.

Case ids (C1, C2, C10, C12, E1-E3, E7, E8) refer to the code-pipeline test cases of this change.
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
from sloperator.store import EventStore

NIC = ZoneInfo("Asia/Nicosia")
LOOP_TASKS = {
    "daily-experiment-finalizer",
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
            "create_app": lambda *_a, **_k: SimpleNamespace(client=AsyncMock()),
            "AsyncSocketModeHandler": SocketHandler,
            "create_health_app": lambda: SimpleNamespace(router=Mock()),
            "create_admin_routes": noop,
            "ClaudeUsageAlert": noop,
            "rerun_interrupted_overdue_sweep": AsyncMock(),
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
