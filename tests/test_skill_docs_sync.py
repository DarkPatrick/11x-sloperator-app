from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.config import Settings
from sloperator.skill_docs_sync import next_run_at, run_once


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
    assert command.await_args_list[0].args[0] == (
        "git",
        "pull",
        "--ff-only",
        "origin",
        "main",
    )
    sync_call = command.await_args_list[1]
    assert sync_call.args[0] == (str(python), str(scripts / "skill_docs_sync.py"), "sync")
    assert sync_call.kwargs["env"]["CLAUDE_BIN"] == str(tmp_path / "claude")
    assert sync_call.kwargs["env"]["CLAUDE_MODEL"] == "claude-test"
    assert sync_call.kwargs["env"]["SKILL_DOCS_PROMPT_PREAMBLE"] == AUTOMATED_RESPONSE_STYLE
