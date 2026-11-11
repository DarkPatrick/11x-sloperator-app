"""Slot rules, catch-up decision and durable slot record of the daily/weekly loops.

Case ids (C3-C9, C11, E4-E6) refer to the code-pipeline test cases of this change.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from sloperator import (
    automation_error_audit,
    daily_activity_digest,
    experiment_design_planner,
    experiment_finalizer,
    mysql_docs_sync,
    scheduler_catchup,
    skill_docs_sync,
)
from sloperator.config import Settings
from sloperator.scheduler_catchup import SlotRunner, previous_slot
from sloperator.store import EventStore

NIC = ZoneInfo("Asia/Nicosia")
AUDIT = "automation-error-audit"
SETTINGS = Settings("UOWNER", "xoxb-test", "xapp-test")


def utc(*parts: int) -> dt.datetime:
    return dt.datetime(*parts, tzinfo=dt.UTC)


def nic(*parts: int) -> dt.datetime:
    return dt.datetime(*parts, tzinfo=NIC)


class Clock:
    """Patched loop clock: a sleep jumps to its target; sleep ``stop_after`` ends the loop."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, now: dt.datetime, stop_after: int = 1):
        self.now = now
        self.stop_after = stop_after
        self.sleeps: list[dt.datetime] = []
        monkeypatch.setattr(scheduler_catchup, "utcnow", lambda: self.now)
        monkeypatch.setattr(scheduler_catchup, "sleep_until", self.sleep_until)

    async def sleep_until(self, target: dt.datetime, _now: dt.datetime) -> None:
        self.sleeps.append(target)
        if len(self.sleeps) >= self.stop_after:
            raise asyncio.CancelledError
        self.now = target.astimezone(dt.UTC)


def new_store(path: Path, records: dict[str, dt.datetime] | None = None) -> EventStore:
    store = EventStore(path)
    store.initialize()
    for job, slot in (records or {}).items():
        store.record_scheduler_slot(job, slot.isoformat())
    return store


def record(store: EventStore, job: str) -> dt.datetime | None:
    value = store.scheduler_slot(job)
    return None if value is None else dt.datetime.fromisoformat(value)


async def run_audit(
    monkeypatch: pytest.MonkeyPatch,
    store: EventStore,
    *,
    enabled: bool = True,
    body: AsyncMock | None = None,
) -> AsyncMock:
    body = body or AsyncMock()
    monkeypatch.setattr(automation_error_audit, "run_once", body)
    with pytest.raises(asyncio.CancelledError):
        await automation_error_audit.run_daily(
            SimpleNamespace(),
            SimpleNamespace(),
            SETTINGS,
            lambda: enabled,
            slots=SlotRunner(AUDIT, store),
        )
    return body


def test_c3_most_recent_past_slot_follows_each_job_schedule() -> None:
    now = utc(2026, 11, 11, 13, 8, 38)
    s = SETTINGS

    def design(t: dt.datetime) -> dt.datetime:
        return experiment_design_planner.next_run_at(
            t, s.experiment_design_timezone, s.experiment_design_hour
        )

    def analytics(t: dt.datetime) -> dt.datetime:
        return experiment_design_planner.next_run_at(
            t, s.experiment_analytics_timezone, s.experiment_analytics_hour
        )

    assert previous_slot(experiment_finalizer.next_run_at, now) == nic(2026, 11, 11, 12)
    assert previous_slot(design, utc(2026, 11, 14, 10)) == nic(2026, 11, 13, 15)
    assert previous_slot(analytics, utc(2026, 11, 15, 20)) == nic(2026, 11, 13, 16)
    assert previous_slot(daily_activity_digest.next_run_at, utc(2026, 11, 16, 8)) == nic(
        2026, 11, 13, 19
    )
    audit = automation_error_audit.next_run_at
    assert previous_slot(audit, utc(2026, 10, 25, 13)) == utc(2026, 10, 25, 12)
    assert previous_slot(audit, utc(2026, 10, 24, 13)) == utc(2026, 10, 24, 11)
    assert previous_slot(skill_docs_sync.next_run_at, now) == utc(2026, 11, 11, 0)
    assert previous_slot(mysql_docs_sync.next_run_at, now) == utc(2026, 11, 8, 3)


async def test_c4_missed_slots_of_the_incident_day_run_once(monkeypatch, tmp_path) -> None:
    store = new_store(
        tmp_path / "db.sqlite3",
        {AUDIT: nic(2026, 11, 10, 14), "experiment-design-planner": nic(2026, 11, 10, 15)},
    )
    clock = Clock(monkeypatch, utc(2026, 11, 11, 12, 15, 28))
    audit_body = await run_audit(monkeypatch, store)
    assert audit_body.await_count == 1
    assert record(store, AUDIT) == nic(2026, 11, 11, 14)
    assert clock.sleeps == [nic(2026, 11, 12, 14)]

    clock = Clock(monkeypatch, utc(2026, 11, 11, 13, 8, 38))
    design_body = AsyncMock()
    monkeypatch.setattr(experiment_design_planner, "run_once", design_body)
    with pytest.raises(asyncio.CancelledError):
        await experiment_design_planner.run_daily(
            SimpleNamespace(),
            SimpleNamespace(),
            SETTINGS,
            slots=SlotRunner("experiment-design-planner", store),
        )
    assert design_body.await_count == 1
    assert record(store, "experiment-design-planner") == nic(2026, 11, 11, 15)
    assert clock.sleeps == [nic(2026, 11, 12, 15)]


async def test_c5_slots_outside_the_window_are_not_caught_up(monkeypatch, tmp_path) -> None:
    store = new_store(
        tmp_path / "db.sqlite3",
        {
            "daily-activity-digest": nic(2026, 11, 10, 19),
            AUDIT: nic(2026, 11, 10, 14),
            "mysql-docs-sync": utc(2026, 11, 1, 3),
        },
    )
    digest_body = AsyncMock()
    monkeypatch.setattr(daily_activity_digest, "run_once", digest_body)
    digest_clock = Clock(monkeypatch, utc(2026, 11, 11, 22, 30))
    with pytest.raises(asyncio.CancelledError):
        await daily_activity_digest.run_weekdays(
            SimpleNamespace(),
            SimpleNamespace(),
            SETTINGS,
            store,
            slots=SlotRunner("daily-activity-digest", store),
        )
    audit_clock = Clock(monkeypatch, utc(2026, 11, 11, 18, 30))
    audit_body = await run_audit(monkeypatch, store)
    mysql_body = AsyncMock()
    monkeypatch.setattr(mysql_docs_sync, "run_once", mysql_body)
    mysql_clock = Clock(monkeypatch, utc(2026, 11, 10, 10))
    with pytest.raises(asyncio.CancelledError):
        await mysql_docs_sync.run_weekly(SETTINGS, slots=SlotRunner("mysql-docs-sync", store))

    assert digest_body.await_count + audit_body.await_count + mysql_body.await_count == 0
    assert digest_clock.sleeps + audit_clock.sleeps + mysql_clock.sleeps == [
        nic(2026, 11, 12, 19),
        nic(2026, 11, 12, 14),
        utc(2026, 11, 15, 3),
    ]


async def test_c6_record_is_written_before_the_run_and_survives_reopen(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "db.sqlite3"
    store = new_store(path, {AUDIT: nic(2026, 11, 10, 14)})
    seen: list[dt.datetime | None] = []

    async def killed_mid_run(*_args: object) -> None:
        seen.append(record(EventStore(path), AUDIT))
        raise asyncio.CancelledError

    Clock(monkeypatch, utc(2026, 11, 11, 12, 15, 28))
    await run_audit(monkeypatch, store, body=AsyncMock(side_effect=killed_mid_run))
    del store

    reopened = EventStore(path)
    clock = Clock(monkeypatch, utc(2026, 11, 11, 12, 20))
    body = await run_audit(monkeypatch, reopened)
    assert seen == [nic(2026, 11, 11, 14)]
    assert record(reopened, AUDIT) == nic(2026, 11, 11, 14)
    assert body.await_count == 0
    assert clock.sleeps == [nic(2026, 11, 12, 14)]


async def test_c7_disabled_slot_is_recorded_and_never_run_later(
    monkeypatch, tmp_path, caplog
) -> None:
    caplog.set_level(logging.INFO)
    store = new_store(tmp_path / "db.sqlite3", {AUDIT: nic(2026, 11, 10, 14)})
    Clock(monkeypatch, utc(2026, 11, 11, 12, 15, 28))
    disabled_body = await run_audit(monkeypatch, store, enabled=False)
    assert record(store, AUDIT) == nic(2026, 11, 11, 14)
    Clock(monkeypatch, utc(2026, 11, 11, 12, 30))
    enabled_body = await run_audit(monkeypatch, store, enabled=True)
    assert disabled_body.await_count + enabled_body.await_count == 0

    # A regular (non catch-up) slot reached while disabled is recorded the same way.
    Clock(monkeypatch, utc(2026, 11, 11, 13), stop_after=2)
    regular_body = await run_audit(monkeypatch, store, enabled=False)
    assert regular_body.await_count == 0
    assert record(store, AUDIT) == nic(2026, 11, 12, 14)
    assert caplog.text.count("Scheduled automation error audit disabled from admin") == 2


async def test_c8_first_start_records_the_last_slot_without_running(monkeypatch, tmp_path) -> None:
    store = new_store(tmp_path / "db.sqlite3")
    clock = Clock(monkeypatch, utc(2026, 11, 11, 12, 15, 28))
    body = await run_audit(monkeypatch, store)
    assert body.await_count == 0
    assert record(store, AUDIT) == nic(2026, 11, 11, 14)
    assert clock.sleeps == [nic(2026, 11, 12, 14)]


class FlakyReadStore(EventStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.read_failures = 1

    def scheduler_slot(self, job_name: str) -> str | None:
        if self.read_failures:
            self.read_failures -= 1
            raise sqlite3.OperationalError("database is locked")
        return super().scheduler_slot(job_name)


class FailingWriteStore(EventStore):
    def record_scheduler_slot(self, job_name: str, slot: str) -> None:
        raise sqlite3.OperationalError("database is locked")


def scheduler_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == scheduler_catchup.__name__ and r.levelno == logging.ERROR
    ]


async def test_c9_unreadable_record_skips_the_slot_and_the_loop_goes_on(
    monkeypatch, tmp_path, caplog
) -> None:
    path = tmp_path / "db.sqlite3"
    new_store(path, {AUDIT: nic(2026, 11, 10, 14)})
    store = FlakyReadStore(path)
    clock = Clock(monkeypatch, utc(2026, 11, 11, 12, 15, 28), stop_after=2)
    body = await run_audit(monkeypatch, store)
    assert len(scheduler_errors(caplog)) == 1
    assert body.await_count == 1  # only the 2026-11-12 slot, reached by the same loop
    assert clock.sleeps[0] == nic(2026, 11, 12, 14)
    assert record(store, AUDIT) == nic(2026, 11, 12, 14)


async def test_c11_regular_slot_fires_once_at_its_time(monkeypatch, tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    store = new_store(tmp_path / "db.sqlite3", {AUDIT: nic(2026, 11, 11, 14)})
    clock = Clock(monkeypatch, utc(2026, 11, 11, 13), stop_after=2)
    body = await run_audit(monkeypatch, store)
    assert "Next automation error audit scheduled for 2026-11-12T14:00:00+02:00" in caplog.text
    assert body.await_count == 1
    assert record(store, AUDIT) == nic(2026, 11, 12, 14)
    assert clock.sleeps == [nic(2026, 11, 12, 14), nic(2026, 11, 13, 14)]


async def test_e4_slot_exactly_six_hours_old_is_still_caught_up(monkeypatch, tmp_path) -> None:
    store = new_store(tmp_path / "db.sqlite3", {AUDIT: nic(2026, 11, 10, 14)})
    Clock(monkeypatch, utc(2026, 11, 11, 18))
    body = await run_audit(monkeypatch, store)
    assert body.await_count == 1


async def test_e5_start_at_the_slot_instant_runs_it_once(monkeypatch, tmp_path) -> None:
    store = new_store(tmp_path / "db.sqlite3", {AUDIT: nic(2026, 11, 10, 14)})
    clock = Clock(monkeypatch, utc(2026, 11, 11, 12))
    body = await run_audit(monkeypatch, store)
    assert body.await_count == 1
    assert clock.sleeps == [nic(2026, 11, 12, 14)]


async def test_e6_unwritable_record_means_no_run(monkeypatch, tmp_path, caplog) -> None:
    path = tmp_path / "db.sqlite3"
    new_store(path, {AUDIT: nic(2026, 11, 10, 14)})
    clock = Clock(monkeypatch, utc(2026, 11, 11, 12, 15, 28))
    body = await run_audit(monkeypatch, FailingWriteStore(path))
    assert len(scheduler_errors(caplog)) == 1
    assert body.await_count == 0
    assert clock.sleeps == [nic(2026, 11, 12, 14)]
