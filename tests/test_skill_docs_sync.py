from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock

import pytest

from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.claude_budget import ClaudeQuotaExceeded
from sloperator.config import Settings
from sloperator.skill_docs_sync import (
    SkillDocsSyncError,
    _run_command,
    next_run_at,
    run_daily,
    run_once,
)


def test_next_run_is_daily_at_midnight_utc() -> None:
    assert next_run_at(dt.datetime(2026, 9, 16, 0, 0, tzinfo=dt.UTC)) == dt.datetime(
        2026, 9, 17, 0, 0, tzinfo=dt.UTC
    )
    assert next_run_at(dt.datetime(2026, 9, 16, 23, 59, tzinfo=dt.UTC)) == dt.datetime(
        2026, 9, 17, 0, 0, tzinfo=dt.UTC
    )


async def test_run_once_updates_main_then_runs_sync(tmp_path, monkeypatch) -> None:
    scripts = tmp_path / "scripts"
    python = tmp_path / ".venv" / "bin" / "python"
    scripts.mkdir()
    python.parent.mkdir(parents=True)
    preflight = scripts / "freshness_preflight.sh"
    preflight.touch()
    (scripts / "skill_docs_sync.py").touch()
    python.touch()
    command = AsyncMock(side_effect=["Already up to date.", "nothing to do"])
    monkeypatch.setattr("sloperator.skill_docs_sync._run_command", command)
    settings = Settings(
        "UOWNER",
        "xoxb-test",
        "xapp-test",
        agent_workspace=tmp_path,
        claude_cli=tmp_path / "claude",
        claude_model="claude-test",
    )

    assert await run_once(settings) == "nothing to do"
    assert command.await_count == 2
    assert command.await_args_list[0].args[0] == (str(preflight),)
    sync_call = command.await_args_list[1]
    assert sync_call.args[0] == (str(python), str(scripts / "skill_docs_sync.py"), "sync")
    assert sync_call.kwargs["env"]["CLAUDE_BIN"] == str(tmp_path / "claude")
    assert sync_call.kwargs["env"]["CLAUDE_MODEL"] == "claude-test"
    assert sync_call.kwargs["env"]["SKILL_DOCS_PROMPT_PREAMBLE"] == AUTOMATED_RESPONSE_STYLE


async def _failing_process(output: str, monkeypatch) -> None:
    """Make asyncio.create_subprocess_exec yield one process that fails with ``output``."""

    class Process:
        returncode = 75

        async def communicate(self) -> tuple[bytes, bytes]:
            return output.encode(), b""

    async def create(*_args, **_kwargs) -> Process:
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)


async def test_run_command_reports_a_spent_allowance_as_a_quota_error(
    tmp_path, monkeypatch
) -> None:
    await _failing_process(
        "[skill-docs 00:00:50] app-store-connect: Claude allowance is spent — "
        "You've hit your weekly limit\n"
        "[skill-docs 00:00:50] SKILL_DOCS_QUOTA_EXHAUSTED; published before the stop: none; "
        "not attempted: 24\n",
        monkeypatch,
    )
    with pytest.raises(ClaudeQuotaExceeded):
        await _run_command(("sync",), cwd=tmp_path)


async def test_run_command_keeps_ordinary_failures_deterministic(tmp_path, monkeypatch) -> None:
    await _failing_process("[skill-docs 00:00:50] FAILED: app-store-connect\n", monkeypatch)
    with pytest.raises(SkillDocsSyncError):
        await _run_command(("sync",), cwd=tmp_path)


async def test_daily_run_waits_for_the_reset_instead_of_losing_the_day(monkeypatch) -> None:
    """A spent allowance must suspend the run, not skip it until tomorrow."""
    attempts: list[str] = []

    async def run_once_stub(_settings: Settings) -> str:
        attempts.append("run")
        if len(attempts) == 1:
            raise ClaudeQuotaExceeded("allowance spent")
        raise asyncio.CancelledError  # end the daily loop once the retry has happened

    waits: list[str] = []

    async def wait_stub(_settings: Settings, *, context: str) -> None:
        waits.append(context)

    monkeypatch.setattr("sloperator.skill_docs_sync.run_once", run_once_stub)
    monkeypatch.setattr("sloperator.agents.wait_for_claude_quota_reset", wait_stub)
    monkeypatch.setattr("sloperator.skill_docs_sync.next_run_at", lambda now: now)
    settings = Settings("UOWNER", "xoxb-test", "xapp-test")

    with pytest.raises(asyncio.CancelledError):
        await run_daily(settings)

    assert attempts == ["run", "run"]
    assert waits == ["Scheduled skill docs sync"]
