"""Slot bookkeeping shared by the embedded daily and weekly scheduler loops.

A slot is one scheduled time of a job under its own rules (hour, timezone, weekday rule). Each
slot fires at most once: firing it, or skipping it because the job is disabled from the admin,
writes a durable per-job record before the run starts. A slot that passed while the service was
down or still starting is fired once after startup if it is still inside the catch-up window.

The recovery gate keeps a job from firing while one of its own runs left unfinished by the
previous process is still being recovered, until the startup recovery chain has finished.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)
CATCH_UP_WINDOW = dt.timedelta(hours=6)
LOOKBACK = dt.timedelta(days=8)
GATE_RECHECK_SECONDS = 60.0

Schedule = Callable[[dt.datetime], dt.datetime]


class SlotStore(Protocol):
    def scheduler_slot(self, job_name: str) -> str | None: ...

    def record_scheduler_slot(self, job_name: str, slot: str) -> None: ...


class RunStore(Protocol):
    def list_interrupted_scheduled_agent_runs(
        self, job_name: str | None = None
    ) -> list[dict[str, object]]: ...


def utcnow() -> dt.datetime:
    """The scheduler loops' notion of now (patched in tests)."""
    return dt.datetime.now(dt.UTC)


async def sleep_until(target: dt.datetime, now: dt.datetime) -> None:
    """Sleep from ``now`` until ``target`` (patched in tests)."""
    await asyncio.sleep(max(0.0, (target.astimezone(dt.UTC) - now).total_seconds()))


def previous_slot(schedule: Schedule, now: dt.datetime) -> dt.datetime | None:
    """Return the most recent slot at or before ``now``.

    ``schedule(t)`` returns the first slot strictly after ``t``; walking forward from a point
    well before the longest gap between slots (a week) visits every slot up to ``now``.
    """
    latest: dt.datetime | None = None
    candidate = schedule(now - LOOKBACK)
    while candidate <= now:
        latest = candidate
        candidate = schedule(candidate)
    return latest


def in_window(
    slot: dt.datetime,
    now: dt.datetime,
    window: dt.timedelta = CATCH_UP_WINDOW,
) -> bool:
    """A slot may fire when at most ``window`` old and not from an earlier day in its timezone."""
    if now - slot > window:
        return False
    return slot.date() >= now.astimezone(slot.tzinfo).date()


class RecoveryGate:
    """Hold each job while its runs that were unfinished at startup are still unfinished."""

    def __init__(
        self,
        store: RunStore,
        pending: Mapping[str, Iterable[str]],
        *,
        recheck_seconds: float | None = None,
    ) -> None:
        self._store = store
        self._pending = {job: frozenset(ids) for job, ids in pending.items() if ids}
        self._recheck_seconds = recheck_seconds
        self._released = asyncio.Event()

    @classmethod
    async def snapshot(
        cls,
        store: RunStore,
        run_job_names: Mapping[str, Iterable[str]],
        *,
        recheck_seconds: float | None = None,
    ) -> RecoveryGate:
        """Note, per job, the run ids that are unfinished right now."""
        rows = await asyncio.to_thread(store.list_interrupted_scheduled_agent_runs)
        owner = {
            run_name: job
            for job, run_names in run_job_names.items()
            for run_name in run_names
        }
        pending: dict[str, set[str]] = {}
        for row in rows:
            job = owner.get(str(row["job_name"]))
            if job is not None:
                pending.setdefault(job, set()).add(str(row["run_id"]))
        return cls(store, pending, recheck_seconds=recheck_seconds)

    def release(self) -> None:
        """The recovery chain has finished: no job is held any more."""
        self._released.set()

    async def _unfinished(self) -> frozenset[str]:
        rows = await asyncio.to_thread(self._store.list_interrupted_scheduled_agent_runs)
        return frozenset(str(row["run_id"]) for row in rows)

    async def wait(self, job_name: str) -> None:
        """Return once the job's interrupted runs are finished or the chain has ended."""
        run_ids = self._pending.get(job_name)
        logged = False
        while run_ids and not self._released.is_set():
            try:
                still_unfinished = run_ids & await self._unfinished()
            except Exception:
                LOGGER.exception("Could not read the recovery state of %s", job_name)
                still_unfinished = run_ids
            if not still_unfinished:
                break
            if not logged:
                LOGGER.info(
                    "Scheduled %s is waiting for the recovery of its interrupted run(s): %s",
                    job_name,
                    ", ".join(sorted(still_unfinished)),
                )
                logged = True
            recheck = (
                GATE_RECHECK_SECONDS if self._recheck_seconds is None else self._recheck_seconds
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._released.wait(), recheck)
        self._pending.pop(job_name, None)


class SlotRunner:
    """Decide which slot a job's loop fires next and record it durably before it runs.

    Without a store the runner behaves like the original loops: sleep to the next slot and
    fire it, with no memory of past slots.
    """

    def __init__(
        self,
        job_name: str,
        store: SlotStore | None = None,
        *,
        gate: RecoveryGate | None = None,
        ready: Callable[[], Awaitable[Any]] | None = None,
        window: dt.timedelta = CATCH_UP_WINDOW,
    ) -> None:
        self.job_name = job_name
        self.store = store
        self.gate = gate
        self.ready = ready
        self.window = window
        self._started = False

    async def next_slot(
        self,
        schedule: Schedule,
        logger: logging.Logger,
        scheduled_message: str,
    ) -> dt.datetime | None:
        """Wait for the next slot to fire; ``None`` means this slot must not run."""
        now = utcnow()
        slot: dt.datetime | None = None
        if not self._started:
            self._started = True
            if self.store is not None:
                slot = await self._catch_up_slot(schedule, now)
        if slot is None:
            slot = schedule(now)
            logger.info(scheduled_message, slot.isoformat())
            await sleep_until(slot, now)
        else:
            LOGGER.info("Catching up the missed %s slot %s", self.job_name, slot.isoformat())
        if self.gate is not None:
            await self.gate.wait(self.job_name)
        if self.ready is not None:
            await self.ready()
        if self.store is None:
            return slot
        return await self._claim(slot)

    async def _read_record(self) -> dt.datetime | None:
        assert self.store is not None
        value = await asyncio.to_thread(self.store.scheduler_slot, self.job_name)
        return None if value is None else dt.datetime.fromisoformat(value)

    async def _write_record(self, slot: dt.datetime) -> None:
        assert self.store is not None
        await asyncio.to_thread(self.store.record_scheduler_slot, self.job_name, slot.isoformat())

    async def _catch_up_slot(self, schedule: Schedule, now: dt.datetime) -> dt.datetime | None:
        slot = previous_slot(schedule, now)
        if slot is None:
            return None
        try:
            recorded = await self._read_record()
            if recorded is None:
                # First start with slot tracking: the previous version may already have run
                # this slot, so it is taken as handled rather than repeated.
                await self._write_record(slot)
                LOGGER.info(
                    "No slot record for %s yet; slot %s recorded as handled without a run",
                    self.job_name,
                    slot.isoformat(),
                )
                return None
        except Exception:
            LOGGER.exception(
                "Could not use the slot record of %s; slot %s is not run",
                self.job_name,
                slot.isoformat(),
            )
            return None
        if recorded >= slot:
            return None
        if not in_window(slot, now, self.window):
            LOGGER.info(
                "Missed %s slot %s is outside the catch-up window; not run",
                self.job_name,
                slot.isoformat(),
            )
            return None
        return slot

    async def _claim(self, slot: dt.datetime) -> dt.datetime | None:
        now = utcnow()
        if not in_window(slot, now, self.window):
            LOGGER.warning(
                "%s slot %s left the catch-up window before it could fire; not run",
                self.job_name,
                slot.isoformat(),
            )
            return None
        try:
            recorded = await self._read_record()
            if recorded is not None and recorded >= slot:
                return None
            await self._write_record(slot)
        except Exception:
            LOGGER.exception(
                "Could not use the slot record of %s; slot %s is not run",
                self.job_name,
                slot.isoformat(),
            )
            return None
        return slot
