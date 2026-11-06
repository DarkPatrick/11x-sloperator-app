import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sloperator.agent_usage import activity_delta, context_for_job, transcript_activity
from sloperator.agents import (
    ActiveAgentRun,
    AgentAuthenticationError,
    AgentExecutionError,
    run_claude,
)
from sloperator.claude_budget import ClaudeBudgetExceeded, transcript_directory
from sloperator.config import Settings
from sloperator.store import AGENT_USAGE_ACTIVITY_COLUMNS, SCHEMA, AgentSession, EventStore


def _assistant(
    path: Path, identifier: str, *, tools: list[tuple[str, str, dict]], cached: int
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = [
        {"type": "tool_use", "id": tid, "name": name, "input": args} for tid, name, args in tools
    ]
    record = {
        "type": "assistant",
        "message": {
            "id": identifier,
            "content": content,
            "usage": {"input_tokens": 1, "cache_read_input_tokens": cached, "output_tokens": 2},
        },
    }
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


def test_activity_delta_counts_only_new_tools_skills_and_children(tmp_path: Path) -> None:
    main = tmp_path / "s1.jsonl"
    _assistant(main, "m1", tools=[("t1", "Bash", {"command": "ls"})], cached=10)
    before = transcript_activity(tmp_path, "s1")

    _assistant(
        main,
        "m2",
        tools=[
            ("t2", "Skill", {"skill": "ug-experiment-calculator"}),
            ("t3", "Bash", {"command": "pwd"}),
            ("t4", "Agent", {"subagent_type": "Explore"}),
        ],
        cached=20,
    )
    _assistant(
        tmp_path / "s1/subagents/agent-a.jsonl",
        "c1",
        tools=[("t5", "Read", {"file_path": "x"})],
        cached=100,
    )
    delta = activity_delta(before, transcript_activity(tmp_path, "s1"))

    assert delta.tool_calls == 4
    assert delta.tool_counts == {"Agent": 1, "Bash": 1, "Read": 1, "Skill": 1}
    assert delta.skills == ("ug-experiment-calculator",)
    assert delta.subagents == 1
    assert delta.subagent_tokens == 103


def test_missing_transcript_gives_empty_activity(tmp_path: Path) -> None:
    activity = transcript_activity(tmp_path, "absent")

    assert activity.tool_uses == {}
    assert activity_delta(activity, activity).tool_calls == 0


def test_existing_database_gets_activity_columns(tmp_path: Path) -> None:
    database = tmp_path / "old.sqlite3"
    added = {f"    {name} {kind}," for name, kind in AGENT_USAGE_ACTIVITY_COLUMNS}
    old_schema = "\n".join(line for line in SCHEMA.splitlines() if line not in added)
    with sqlite3.connect(database) as connection:
        connection.executescript(old_schema)
        assert "tool_calls" not in {
            row[1] for row in connection.execute("PRAGMA table_info(agent_usage_invocations)")
        }

    EventStore(database).initialize()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_usage_invocations)")
        }
    assert {
        "tool_calls",
        "tool_counts",
        "skills",
        "subagent_calls",
        "subagent_tokens",
        "outcome",
        "workspace_commit",
        "sloperator_commit",
    } <= columns


def _setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Settings, AgentSession, EventStore, Path]:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    database = tmp_path / "archive.sqlite3"
    store = EventStore(database)
    store.initialize()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        slack_user_id="U123",
        bot_token="test",
        app_token="test",
        agent_workspace=workspace,
        database_path=database,
    )
    session = AgentSession(
        channel_id="scheduled",
        thread_ts="run-1",
        provider="claude",
        model="opus",
        external_session_id="session-1",
        status="queued",
        turn_count=0,
        last_error=None,
    )
    return settings, session, store, transcript_directory(workspace)


async def test_completed_run_records_tools_skills_and_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, session, store, transcripts = _setup(tmp_path, monkeypatch)

    async def provider(*_args: object, **_kwargs: object) -> tuple[int, str, str]:
        _assistant(
            transcripts / "session-1.jsonl",
            "m1",
            tools=[
                ("t1", "Skill", {"skill": "time-series-research"}),
                ("t2", "Bash", {"command": "x"}),
            ],
            cached=50,
        )
        payload = {
            "result": "done",
            "session_id": "session-1",
            "num_turns": 1,
            "usage": {"input_tokens": 1, "cache_read_input_tokens": 50, "output_tokens": 2},
        }
        return 0, json.dumps(payload), ""

    monkeypatch.setattr("sloperator.agents._run_process", provider)
    await run_claude(
        settings,
        session,
        "Work",
        ActiveAgentRun("claude"),
        usage_context=context_for_job("experiment-finalizer-preparer", source_run_id="run-1"),
    )

    row = store.agent_usage_report()["recent"][0]
    assert row["outcome"] == "completed"
    assert row["tool_calls"] == 2
    assert json.loads(row["tool_counts"]) == {"Bash": 1, "Skill": 1}
    assert json.loads(row["skills"]) == ["time-series-research"]
    assert row["subagent_calls"] == 0


async def test_failed_run_keeps_transcript_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, session, store, transcripts = _setup(tmp_path, monkeypatch)

    async def provider(*_args: object, **_kwargs: object) -> tuple[int, str, str]:
        _assistant(
            transcripts / "session-1.jsonl",
            "m1",
            tools=[("t1", "Bash", {"command": "x"})],
            cached=500,
        )
        return 0, "not json", "Ignoring 47 permissions.allow entries"

    monkeypatch.setattr("sloperator.agents._run_process", provider)
    with pytest.raises(AgentExecutionError):
        await run_claude(
            settings,
            session,
            "Work",
            ActiveAgentRun("claude"),
            usage_context=context_for_job("experiment-config-check", source_run_id="run-1"),
        )

    row = store.agent_usage_report()["recent"][0]
    assert row["status"] == "failed"
    assert row["usage_source"] == "transcript_delta"
    assert row["total_tokens"] == 503
    assert row["tool_calls"] == 1


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ClaudeBudgetExceeded("budget"), "budget_stopped"),
        (AgentAuthenticationError("claude", "logged out"), "auth_failed"),
        (RuntimeError("boom"), "failed"),
    ],
)
async def test_outcome_names_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException, outcome: str
) -> None:
    settings, session, store, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("sloperator.agents._run_process", AsyncMock(side_effect=error))

    with pytest.raises(type(error)):
        await run_claude(
            settings,
            session,
            "Work",
            ActiveAgentRun("claude"),
            usage_context=context_for_job("experiment-design-preparer", source_run_id="run-1"),
        )

    assert store.agent_usage_report()["recent"][0]["outcome"] == outcome
