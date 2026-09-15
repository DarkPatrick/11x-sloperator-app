import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.config import Settings
from sloperator.operations_cron import command_spec, unwrap_command, wrap_crontab
from sloperator.operations_log import OperationsCollector, render_event
from sloperator.operations_store import (
    OperationsStore,
    configure_operations,
    observed,
    outcome,
    redact,
)
from sloperator.store import EventStore


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "sloperator.sqlite3"
    EventStore(path).initialize()
    return path


def test_transactional_transitions_are_not_lost_between_polls(database):
    store = OperationsStore(database)
    with store.connect() as c:
        c.execute(
            "INSERT INTO scheduled_agent_runs(run_id,job_name,provider,model,prompt) VALUES('1','new-future-job','claude','opus','private prompt')"  # noqa: E501
        )
        c.execute(
            "UPDATE scheduled_agent_runs SET status='completed',result_text='Experiment finalisation failed: frozen mirror' WHERE run_id='1'"  # noqa: E501
        )
    events = store.pending()
    assert [r["status"] for r in events] == ["running", "completed"]
    assert "private prompt" not in json.dumps(events)
    assert outcome(events[-1]["status"], events[-1]["detail"])[0] == "❌"
    with pytest.raises(RuntimeError), store.connect() as c:
        c.execute("UPDATE scheduled_agent_runs SET status='failed' WHERE run_id='1'")
        raise RuntimeError("rollback")
    assert len(store.pending()) == 2


def test_slack_requests_recoveries_and_delivery_covered(database):
    store = OperationsStore(database)
    with store.connect() as c:
        c.execute(
            "INSERT INTO agent_requests(channel_id,message_ts,thread_ts) VALUES('C1','1.1:new-trigger','1.1')"  # noqa: E501
        )
        c.execute("UPDATE agent_requests SET status='interrupted'")
        c.execute("UPDATE agent_requests SET status='queued'")
        c.execute("UPDATE agent_requests SET status='completed'")
        c.execute(
            "INSERT INTO delivered_agent_artifacts(channel_id,thread_ts,fingerprint) VALUES('C1','1.1','zip')"  # noqa: E501
        )
    assert [e["status"] for e in store.pending()] == [
        "queued",
        "interrupted",
        "queued",
        "completed",
        "delivered",
    ]


async def test_failed_slack_delivery_keeps_queue_across_restart(database):
    store = OperationsStore(database)
    store.emit("cron", "failed", "token=private xoxb-private <@UOWNER>")
    settings = Settings(slack_user_id="U1", bot_token="x", app_token="y", database_path=database)
    client = SimpleNamespace(chat_postMessage=AsyncMock(side_effect=OSError("network")))
    first = OperationsCollector(settings, client, "CLOG")
    with pytest.raises(OSError):
        await first.publish_pending()
    assert len(store.pending()) == 1
    client.chat_postMessage.side_effect = None
    second = OperationsCollector(settings, client, "CLOG")
    assert await second.publish_pending() == 1
    assert not store.pending()
    sent = client.chat_postMessage.call_args.kwargs["text"]
    assert "xoxb-private" not in sent and "<@UOWNER>" not in sent and "token=private" not in sent
    assert list((database.parent / "operations" / "deliveries").glob("*.json"))


async def test_usage_is_separate_durable_half_hour_slot(database, monkeypatch):
    from sloperator.claude_usage import ClaudeUsage

    store = OperationsStore(database)
    settings = Settings(slack_user_id="U1", bot_token="x", app_token="y", database_path=database)
    collector = OperationsCollector(settings, None, "CLOG")
    reader = AsyncMock(return_value=ClaudeUsage(20, 70, "today 16:00", "Friday"))
    monkeypatch.setattr("sloperator.operations_log.read_usage", reader)
    monkeypatch.setattr("sloperator.operations_log.time.time", lambda: 3600)
    await collector.usage()
    await OperationsCollector(settings, None, "CLOG").usage()
    assert reader.await_count == 1
    assert len(store.pending()) == 1
    monkeypatch.setattr("sloperator.operations_log.time.time", lambda: 5400)
    reader.side_effect = TimeoutError("private diagnostic")
    await collector.usage()
    assert reader.await_count == 2
    assert store.pending()[-1]["status"] == "failed"
    assert "Значения неизвестны" in store.pending()[-1]["detail"]


async def test_low_level_boundary_covers_helpers_failures_and_cancel(database):
    configure_operations(database)
    try:

        @observed("helper-agent")
        async def helper(fail=False):
            if fail:
                raise ValueError("bad output")
            return "A complete result"

        await helper()
        with pytest.raises(ValueError):
            await helper(True)

        @observed("cancel-agent")
        async def cancelled():
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await cancelled()
        assert [e["status"] for e in OperationsStore(database).pending(20)] == [
            "running",
            "returned",
            "running",
            "failed",
            "running",
            "cancelled",
        ]
    finally:
        configure_operations(None)


def test_cron_wrapper_preserves_environment_exit_and_redirects(tmp_path):
    output = tmp_path / "original.log"
    command = f"TEST_CRON_VALUE=hello /bin/sh -c 'echo $TEST_CRON_VALUE; exit 7' >> {shlex.quote(str(output))} 2>&1"  # noqa: E501
    text = f"# >>> ug-ai-analyst:test >>>\n*/5 * * * * {command}\n# <<< ug-ai-analyst:test <<<\n"
    updated, names = wrap_crontab(
        text, tmp_path / "operations" / "cron-specs", Path(sys.executable)
    )
    assert names == ["ug-ai-analyst:test"]
    wrapper = updated.splitlines()[1].split(maxsplit=5)[5]
    assert unwrap_command(wrapper) == command
    assert (
        wrap_crontab(updated, tmp_path / "operations" / "cron-specs", Path(sys.executable))[0]
        == updated
    )
    result = subprocess.run(shlex.split(wrapper), capture_output=True, text=True)
    assert result.returncode == 7, result.stderr
    assert output.read_text() == "hello\n"
    events = [
        json.loads(p.read_text())
        for p in sorted((tmp_path / "operations" / "spool").glob("*.json"))
    ]
    assert [e["status"] for e in events] == ["running", "failed"]
    assert Path(events[-1]["reference"]).is_file()
    assert "hello" in Path(events[-1]["reference"]).read_text()


def test_disabled_and_new_unmanaged_crons_covered(tmp_path):
    text = "# sloperator-disabled: 0 8 * * * true\n@reboot echo hello\n"
    updated, names = wrap_crontab(text, tmp_path / "specs", Path(sys.executable))
    assert updated.startswith("# sloperator-disabled: 0 8 * * * ")
    assert len(names) == 2
    assert command_spec(updated.splitlines()[1].split(maxsplit=1)[1])


def test_percent_cron_fails_visibly_instead_of_changing_semantics(tmp_path):
    with pytest.raises(ValueError, match="stdin"):
        wrap_crontab("* * * * * cat % data\n", tmp_path / "specs", Path(sys.executable))


def test_redaction_and_blocked_results():
    assert "private" not in redact("Bearer private https://user:private@host/ token=private")
    e = {
        "source": "a",
        "status": "completed",
        "detail": "Blocked: mirror unavailable",
        "reference": "C123/123.456",
        "created_at": "2026-09-15 01:02:03",
    }
    assert "блокировка" in render_event(e)
    assert "https://slack.com/archives/C123/p123456" in render_event(e)


async def test_usage_gets_its_own_post(database):
    store = OperationsStore(database)
    store.emit("cron", "running")
    store.emit("Claude", "usage", "session 50%, week 70%")
    store.emit("cron", "completed")
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    settings = Settings(slack_user_id="U", bot_token="b", app_token="a", database_path=database)
    collector = OperationsCollector(settings, client, "CLOG")
    assert await collector.publish_pending() == 1
    assert await collector.publish_pending() == 1
    assert "session 50%" in client.chat_postMessage.call_args.kwargs["text"]
    assert "*cron*" not in client.chat_postMessage.call_args.kwargs["text"]
    assert await collector.publish_pending() == 1


def test_journal_covers_runtime_systemd_and_unwrapped_cron(database, monkeypatch):
    import sloperator.operations_log as module

    settings = Settings(slack_user_id="U", bot_token="b", app_token="a", database_path=database)
    collector = OperationsCollector(settings, None, "CLOG")
    collector.store.checkpoint("journal", "old-cursor")
    rows = [
        {
            "__CURSOR": "1",
            "_SYSTEMD_UNIT": "sloperator.service",
            "MESSAGE": "2026 ERROR sloperator.experiment_finalizer: failed before publication",
        },
        {
            "__CURSOR": "2",
            "UNIT": "ug-ai-analyst-update.service",
            "MESSAGE": "Main process exited, status=1/FAILURE",
        },
        {"__CURSOR": "3", "SYSLOG_IDENTIFIER": "CRON", "MESSAGE": "(egor) CMD (unwrapped-new-job)"},
        {
            "__CURSOR": "4",
            "_SYSTEMD_UNIT": "sloperator-operations.service",
            "MESSAGE": "Slack log delivery failed",
        },
        {"__CURSOR": "5", "_SYSTEMD_UNIT": "ssh.service", "MESSAGE": "unrelated auth message"},
    ]

    def run(*a, **k):
        return SimpleNamespace(
            returncode=0, stdout="\n".join(json.dumps(r) for r in rows), stderr=""
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    collector.ingest_journal()
    collector.ingest_journal()
    events = collector.store.pending()
    assert len(events) == 3
    assert events[0]["status"] == "failed"
    assert collector.store.cursor("journal") == "5"


def test_detached_retry_and_rotated_log_covered(database, tmp_path):
    logs = tmp_path / "analyst" / "scripts" / "logs"
    logs.mkdir(parents=True)
    path = logs / "monitor.cron.out.log"
    path.write_text("old run\n")
    settings = Settings(
        slack_user_id="U",
        bot_token="b",
        app_token="a",
        database_path=database,
        agent_workspace=tmp_path / "analyst",
    )
    collector = OperationsCollector(settings, None, "CLOG")
    collector.ingest_retry_logs()
    with path.open("a") as f:
        f.write("[today] [cron_retry:monitor.retry.123] running: /private/command\n")
        f.write("[today] [cron_retry:monitor.retry.123] child exited rc=75\n")
    collector.ingest_retry_logs()
    collector.ingest_retry_logs()
    assert [e["status"] for e in collector.store.pending()] == ["running", "failed"]
    path.rename(logs / "old.log")
    path.write_text("[today] [cron_retry:monitor.retry.124] child exited rc=0\n")
    collector.ingest_retry_logs()
    assert collector.store.pending()[-1]["status"] == "completed"


def test_lost_cron_after_process_death_is_reported(database):
    from sloperator.operations_cron import spool

    settings = Settings(slack_user_id="U", bot_token="b", app_token="a", database_path=database)
    collector = OperationsCollector(settings, None, "CLOG")
    spool(
        collector.directory / "spool",
        {
            "source": "cron",
            "status": "running",
            "run_id": "dead",
            "pid": 999999999,
            "started": 1,
            "reference": "raw.log",
        },
    )
    collector.ingest_spool()
    assert [e["status"] for e in collector.store.pending()] == ["running", "interrupted"]
    collector.ingest_spool()
    assert len(collector.store.pending()) == 2


async def test_shared_slack_boundary_captures_direct_publications_and_errors(database, monkeypatch):
    from slack_sdk.web.async_client import AsyncWebClient

    from sloperator.operations_slack import ObservedSlackClient

    mock = AsyncMock(return_value={"channel": "C123", "ts": "123.456"})
    monkeypatch.setattr(AsyncWebClient, "api_call", mock)
    configure_operations(database)
    try:
        client = ObservedSlackClient(token="test")
        await client.api_call(
            "chat.postMessage",
            json={"channel": "C123", "text": "Experiment finalisation failed: stale mirror"},
        )
        mock.side_effect = OSError("network")
        with pytest.raises(OSError):
            await client.api_call("chat.postMessage", json={"channel": "C123", "text": "x"})
        assert [e["status"] for e in OperationsStore(database).pending()] == ["delivered", "failed"]
    finally:
        configure_operations(None)


async def test_bolt_per_request_clients_are_observed():
    from slack_bolt.context.async_context import AsyncBoltContext
    from slack_sdk.web.async_client import AsyncWebClient

    from sloperator.operations_slack import ObservedSlackClient, ObserveRequestClient

    context = AsyncBoltContext()
    original = AsyncWebClient(token="test-token", team_id="T123", timeout=17)
    context["client"] = original
    next_handler = AsyncMock(return_value="ok")
    assert await ObserveRequestClient()(context, next_handler) == "ok"
    assert isinstance(context.client, ObservedSlackClient)
    assert context.client.token == "test-token"
    assert context.client.timeout == 17
    assert original is not context.client
