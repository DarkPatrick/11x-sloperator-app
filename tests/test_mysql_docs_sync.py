from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.claude_budget import ClaudeQuotaExceeded
from sloperator.config import Settings
from sloperator.mysql_docs_sync import MysqlDocsSyncError, _run_command, next_run_at, run_once
from sloperator.scheduled_jobs import EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME


def test_next_run_is_the_next_sunday_three_utc() -> None:
    # 2026-11-05 is a Thursday
    assert next_run_at(dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.UTC)) == dt.datetime(
        2026, 11, 8, 3, 0, tzinfo=dt.UTC
    )
    # Sunday before 03:00 -> same day; at or after 03:00 -> next week
    assert next_run_at(dt.datetime(2026, 11, 8, 2, 59, tzinfo=dt.UTC)) == dt.datetime(
        2026, 11, 8, 3, 0, tzinfo=dt.UTC
    )
    assert next_run_at(dt.datetime(2026, 11, 8, 3, 0, tzinfo=dt.UTC)) == dt.datetime(
        2026, 11, 15, 3, 0, tzinfo=dt.UTC
    )


def test_job_is_registered_for_the_admin() -> None:
    job = EMBEDDED_SCHEDULED_JOBS_BY_JOB_NAME["mysql-docs-sync"]
    assert job.display_name == "mysql-docs-sync (sloperator.service)"
    assert job.schedule(None) == "weekly Sun 03:00 UTC"  # type: ignore[arg-type]


async def test_run_once_updates_main_then_runs_sync(tmp_path, monkeypatch) -> None:
    scripts = tmp_path / "scripts"
    (scripts / "mysql_docs").mkdir(parents=True)
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    preflight = scripts / "freshness_preflight.sh"
    for path in (preflight, scripts / "mysql_docs" / "sync.py", python):
        path.touch()
    command = AsyncMock(side_effect=["Already up to date.", "done"])
    monkeypatch.setattr("sloperator.mysql_docs_sync._run_command", command)
    settings = Settings(
        "UOWNER", "xoxb-test", "xapp-test",
        agent_workspace=tmp_path, claude_cli=tmp_path / "claude", claude_model="claude-test",
    )

    assert await run_once(settings) == "done"
    assert command.await_args_list[0].args[0] == (str(preflight),)
    sync_call = command.await_args_list[1]
    assert sync_call.args[0] == (str(python), str(scripts / "mysql_docs" / "sync.py"), "sync")
    assert sync_call.kwargs["env"]["UG_SKIP_PREFLIGHT"] == "1"
    assert sync_call.kwargs["env"]["MYSQL_DOCS_PROMPT_PREAMBLE"] == AUTOMATED_RESPONSE_STYLE


async def test_run_once_refuses_a_workspace_without_the_script(tmp_path) -> None:
    settings = Settings("UOWNER", "xoxb-test", "xapp-test", agent_workspace=tmp_path)
    with pytest.raises(MysqlDocsSyncError):
        await run_once(settings)


async def _process(output: str, code: int, monkeypatch) -> None:
    class Process:
        returncode = code

        async def communicate(self) -> tuple[bytes, bytes]:
            return output.encode(), b""

    async def create(*_args, **_kwargs) -> Process:
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)


async def test_quota_sentinel_becomes_a_quota_error(tmp_path, monkeypatch) -> None:
    await _process("[mysql-docs] TEMPFAIL ...\nMYSQL_DOCS_QUOTA_EXHAUSTED", 75, monkeypatch)
    with pytest.raises(ClaudeQuotaExceeded):
        await _run_command(("python", "sync.py", "sync"), cwd=tmp_path)


async def test_other_failures_are_sync_errors(tmp_path, monkeypatch) -> None:
    await _process("[mysql-docs] user: NOT published", 1, monkeypatch)
    with pytest.raises(MysqlDocsSyncError):
        await _run_command(("python", "sync.py", "sync"), cwd=tmp_path)
