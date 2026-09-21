import json
from pathlib import Path
from unittest.mock import AsyncMock

from sloperator.agent_usage import (
    context_for_job,
    parse_claude_usage,
    parse_codex_usage,
    slack_agent_name,
    transcript_usage,
)
from sloperator.agents import ActiveAgentRun, run_claude
from sloperator.config import Settings
from sloperator.store import AgentSession, EventStore


def test_logical_agent_names_split_workflow_roles() -> None:
    worker = context_for_job("experiment-finalizer-preparer", source_run_id="run-1")
    reviewer = context_for_job("experiment-finalizer-reviewer")

    assert (worker.agent_name, worker.workflow, worker.role) == (
        "experiment-finalizer/worker",
        "experiment-finalizer",
        "worker",
    )
    assert reviewer.agent_name == "experiment-finalizer/reviewer"
    assert slack_agent_name("experiment-finalizer-reviewer") == "experiment-finalizer/slack"


def test_provider_usage_parsers_keep_cache_separate() -> None:
    claude = parse_claude_usage(
        {
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30,
                "output_tokens": 4,
            },
            "total_cost_usd": 1.25,
            "num_turns": 3,
        }
    )
    codex = parse_codex_usage(
        {"usage": {"inputTokens": 11, "cachedInputTokens": 12, "outputTokens": 13}}
    )

    assert claude is not None and claude.total_tokens == 64
    assert claude.cost_usd == 1.25
    assert codex is not None and codex.total_tokens == 36


def _transcript_record(path: Path, identifier: str, *, cached: int, output: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": identifier,
                        "usage": {
                            "input_tokens": 2,
                            "cache_read_input_tokens": cached,
                            "output_tokens": output,
                        },
                    },
                }
            )
            + "\n"
        )


def test_transcript_usage_includes_child_agents_without_double_counting(tmp_path) -> None:
    _transcript_record(tmp_path / "session.jsonl", "root", cached=10, output=3)
    _transcript_record(tmp_path / "session.jsonl", "root", cached=10, output=5)
    _transcript_record(
        tmp_path / "session/subagents/worker.jsonl", "child", cached=20, output=7
    )

    usage = transcript_usage(tmp_path, "session")

    assert usage.input_tokens == 4
    assert usage.cache_read_input_tokens == 30
    assert usage.output_tokens == 12
    assert usage.total_tokens == 46


async def test_claude_invocation_is_persisted_and_aggregated(tmp_path, monkeypatch) -> None:
    database = tmp_path / "archive.sqlite3"
    store = EventStore(database)
    store.initialize()
    settings = Settings(
        slack_user_id="U123",
        bot_token="test",
        app_token="test",
        agent_workspace=tmp_path,
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
    monkeypatch.setattr(
        "sloperator.agents._run_process",
        AsyncMock(
            return_value=(
                0,
                json.dumps(
                    {
                        "result": "done",
                        "session_id": "session-1",
                        "usage": {
                            "input_tokens": 100,
                            "cache_creation_input_tokens": 20,
                            "cache_read_input_tokens": 300,
                            "output_tokens": 40,
                        },
                        "total_cost_usd": 0.75,
                        "num_turns": 2,
                    }
                ),
                "",
            )
        ),
    )

    result = await run_claude(
        settings,
        session,
        "Work",
        ActiveAgentRun("claude"),
        usage_context=context_for_job("experiment-finalizer-preparer", source_run_id="run-1"),
    )

    assert result.text == "done"
    report = store.agent_usage_report()
    aggregate = report["agents"][0]
    assert aggregate["agent_name"] == "experiment-finalizer/worker"
    assert aggregate["invocations"] == aggregate["completed"] == 1
    assert aggregate["running"] == aggregate["failed"] == 0
    assert aggregate["total_tokens"] == aggregate["avg_tokens"] == 460
    assert aggregate["cost_usd"] == 0.75
    assert report["recent"][0]["usage_source"] == "provider_json"


def test_jira_usage_report_identifies_task_from_persisted_run(tmp_path) -> None:
    from sloperator.store import EventStore

    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.create_scheduled_agent_run(
        "jira-run", job_name="jira-task-reviewer", provider="claude", model="opus",
        external_session_id=None, prompt="You own Jira task UMN-13308.",
    )
    store.start_agent_usage_invocation(
        "invocation", source_run_id="jira-run", agent_name="jira-task/reviewer",
        workflow="jira-task", role="reviewer", source="scheduled",
        provider="claude", model="opus", external_session_id=None,
    )
    store.finish_agent_usage_invocation(
        "invocation", status="completed", usage_source="provider_json", total_tokens=123,
    )

    report = store.agent_usage_report()
    assert report["recent"][0]["task_key"] == "UMN-13308"
    assert report["jira_tasks"] == [{
        "task_key": "UMN-13308", "agent_name": "jira-task/reviewer",
        "invocations": 1, "total_tokens": 123,
        "last_started_at": report["recent"][0]["started_at"],
    }]


def test_confluence_usage_groups_runs_by_project_page(tmp_path) -> None:
    from sloperator.store import EventStore

    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    page = "https://alice.mu.se/pages/viewpage.action?pageId=838603095"
    parent = "https://alice.mu.se/spaces/CRO/pages/103614364/Research+Sandbox"
    for run_id, task_key, prompt, result, tokens in (
        ("one", "UMN-13308", f"You own Jira task UMN-13308. {parent}",
         f"Done: [New versions screen]({page})", 123),
        ("two", "UMN-13310", f"You own Jira task UMN-13310. {parent} {page}",
         "Done", 456),
        ("unlinked", "UMN-99999", f"You own Jira task UMN-99999. {parent}",
         "Done", 789),
    ):
        store.create_scheduled_agent_run(
            run_id, job_name="jira-task-reviewer", provider="claude", model="opus",
            external_session_id=None, prompt=prompt,
        )
        store.finish_scheduled_agent_run(run_id, status="completed", result_text=result)
        store.start_agent_usage_invocation(
            run_id + "-usage", source_run_id=run_id,
            agent_name="jira-task/reviewer", workflow="jira-task", role="reviewer",
            source="scheduled", provider="claude", model="opus", external_session_id=None,
        )
        store.finish_agent_usage_invocation(
            run_id + "-usage", status="completed", total_tokens=tokens,
        )

    projects = store.agent_usage_report()["confluence_projects"]
    assert len(projects) == 1
    assert projects[0]["page_id"] == "838603095"
    assert projects[0]["page_url"] == page
    assert projects[0]["invocations"] == 2
    assert projects[0]["total_tokens"] == 579
