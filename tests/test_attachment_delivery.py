import asyncio
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sloperator.agents import AgentOrchestrator, AgentRunResult
from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.bot import create_app
from sloperator.config import Settings
from sloperator.store import EventStore


def setup_orchestrator(tmp_path):
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        agent_workspace=tmp_path,
        anomaly_alert_channel="CALERT",
    )
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    return settings, store, AgentOrchestrator(settings, store)


@pytest.mark.parametrize("text", ["Explain this chart", ""])
async def test_file_share_and_mention_reach_worker_once(tmp_path, monkeypatch, text):
    settings, store, orchestrator = setup_orchestrator(tmp_path)
    store.create_agent_session("CALERT", "1.1", "claude", "opus", "session-1")
    runner = AsyncMock(return_value=AgentRunResult("session-1", "The chart shows a change."))
    monkeypatch.setattr("sloperator.agents.run_claude", runner)

    async def download(url, token, destination):
        destination.write_bytes(b"real-attachment-bytes")

    monkeypatch.setattr("sloperator.slack_files.download_slack_file", download)
    client = SimpleNamespace(
        token="secret",
        chat_postMessage=AsyncMock(),
        files_upload_v2=AsyncMock(),
        files_info=AsyncMock(
            return_value={
                "file": {
                    "name": "chart.png",
                    "url_private": "https://files.slack.com/private",
                }
            }
        ),
    )
    app = create_app(settings, store, orchestrator, MagicMock())
    message_handler = app._async_listeners[0].ack_function
    mention_handler = app._async_listeners[1].ack_function
    event = {
        "type": "message",
        "subtype": "file_share",
        "channel": "CALERT",
        "user": "UOWNER",
        "thread_ts": "1.1",
        "ts": "1.2",
        "text": text,
        "files": [{"id": "F123", "name": "chart.png"}],
    }
    await message_handler(event=event, client=client)
    await mention_handler(event={**event, "type": "app_mention"}, client=client)
    await orchestrator.drain()
    runner.assert_awaited_once()
    client.files_info.assert_awaited_once()
    prompt = runner.await_args.args[2]
    assert "chart.png" in prompt
    assert "SLACK ATTACHMENTS" in prompt
    assert AUTOMATED_RESPONSE_STYLE in prompt
    assert "secret" not in prompt
    # The actual worker input references bytes on disk, not just Slack metadata.
    staged = list((tmp_path / "output" / "slack_inputs").rglob("*.png"))
    assert staged[0].read_bytes() == b"real-attachment-bytes"
    assert str(staged[0]) in prompt


@pytest.mark.parametrize(
    "event_change",
    [
        {"subtype": "message_changed"},
        {"subtype": "message_deleted"},
        {"bot_id": "B123"},
        {"user": "UUNAUTHORIZED"},
        {"channel": "COTHER"},
    ],
)
async def test_attachment_support_preserves_message_filters(tmp_path, event_change):
    settings, store, _ = setup_orchestrator(tmp_path)
    orchestrator = SimpleNamespace(submit=AsyncMock())
    app = create_app(settings, store, orchestrator, MagicMock())
    event = {
        "type": "message",
        "subtype": "file_share",
        "channel": "CALERT",
        "user": "UOWNER",
        "thread_ts": "1.1",
        "ts": "1.2",
        "text": "Look at this",
        "files": [{"id": "F123"}],
        **event_change,
    }
    await app._async_listeners[0].ack_function(event=event, client=MagicMock())
    orchestrator.submit.assert_not_awaited()


async def test_attachment_can_steer_running_agent(tmp_path, monkeypatch):
    _, _, orchestrator = setup_orchestrator(tmp_path)
    # Use the public steering contract without starting an external CLI.
    steer = AsyncMock(return_value=True)
    orchestrator._active_runs[("CALERT", "1.1")] = SimpleNamespace(steer=steer)
    monkeypatch.setattr(
        "sloperator.agents.attachment_prompt",
        AsyncMock(return_value="\nSLACK ATTACHMENTS: output/slack_inputs/chart.png"),
    )
    result = await orchestrator.submit(
        MagicMock(),
        channel_id="CALERT",
        thread_ts="1.1",
        message_ts="1.2",
        text="Use this chart",
        files=[{"id": "F123"}],
    )
    assert result == "steered"
    assert "chart.png" in steer.await_args.args[0]


def write_zip(path, content, date=(2026, 1, 1, 0, 0, 0)):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("report.txt", date_time=date), content)


async def test_archive_dedup_survives_restart_and_repackaging(tmp_path):
    settings, store, orchestrator = setup_orchestrator(tmp_path)
    archive = tmp_path / "analysis.zip"
    write_zip(archive, "First finding")
    client = SimpleNamespace(chat_postMessage=AsyncMock(), files_upload_v2=AsyncMock())
    await orchestrator._reply_prepared(client, "C123", "1.1", "First answer", archive)
    # Same evidence in a renamed archive, with different ZIP metadata, after restart.
    repackaged = tmp_path / "new-name.zip"
    write_zip(repackaged, "First finding", (2026, 2, 2, 0, 0, 0))
    restarted = AgentOrchestrator(settings, EventStore(store.path))
    await restarted._reply_prepared(client, "C123", "1.1", "Clarification", repackaged)
    assert client.chat_postMessage.await_count == 2
    assert client.files_upload_v2.await_count == 1
    write_zip(repackaged, "New evidence and corrected finding")
    await restarted._reply_prepared(client, "C123", "1.1", "Updated analysis", repackaged)
    assert client.files_upload_v2.await_count == 2
    await restarted._reply_prepared(client, "C123", "2.1", "Other thread", repackaged)
    assert client.files_upload_v2.await_count == 3


@pytest.mark.parametrize("update", [False, True])
async def test_followup_only_attaches_current_turn_artifact(tmp_path, monkeypatch, update):
    _, store, orchestrator = setup_orchestrator(tmp_path)
    store.create_agent_session("CALERT", "1.1", "claude", "opus", "session-1")
    store.finish_agent_turn("CALERT", "1.1", "session-1")
    archive = tmp_path / "analysis.zip"
    write_zip(archive, "Old report from before deployment")
    os.utime(archive, (1, 1))

    async def run(*args, **kwargs):
        if update:
            write_zip(archive, "Corrected evidence")
        return AgentRunResult("session-1", "Answer\nSLOPERATOR_ARTIFACT: analysis.zip")

    monkeypatch.setattr("sloperator.agents.run_claude", run)
    client = SimpleNamespace(chat_postMessage=AsyncMock(), files_upload_v2=AsyncMock())
    await orchestrator.submit(
        client,
        channel_id="CALERT",
        thread_ts="1.1",
        message_ts="1.2",
        text="Clarify the cause",
        show_status=False,
    )
    await orchestrator.drain()
    assert client.files_upload_v2.await_count == int(update)
    client.chat_postMessage.assert_awaited_once()


async def test_failed_upload_does_not_mark_archive_delivered(tmp_path):
    _, _, orchestrator = setup_orchestrator(tmp_path)
    archive = tmp_path / "analysis.zip"
    write_zip(archive, "Evidence")
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(),
        files_upload_v2=AsyncMock(side_effect=[RuntimeError("temporary failure"), {}]),
    )
    with pytest.raises(RuntimeError):
        await orchestrator._reply_prepared(client, "C123", "1.1", "Answer", archive)
    await orchestrator._reply_prepared(client, "C123", "1.1", "Answer", archive)
    assert client.files_upload_v2.await_count == 2


async def test_restart_recovers_attachment_download_before_worker_started(tmp_path, monkeypatch):
    settings, store, orchestrator = setup_orchestrator(tmp_path)
    staging = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr("sloperator.agents.attachment_prompt", staging)
    client = SimpleNamespace(chat_postMessage=AsyncMock(), files_upload_v2=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await orchestrator.submit(
            client,
            channel_id="CALERT",
            thread_ts="1.1",
            message_ts="1.2",
            text="Inspect this image",
            files=[{"id": "F123"}],
            show_status=False,
        )
    pending = store.list_interrupted_durable_agent_runs()
    assert len(pending) == 1
    assert pending[0]["options"]["files"] == [{"id": "F123"}]
    staging.side_effect = None
    staging.return_value = "\nSLACK ATTACHMENTS: output/slack_inputs/recovered.png"
    runner = AsyncMock(return_value=AgentRunResult("session-1", "Image inspected"))
    monkeypatch.setattr("sloperator.agents.run_claude", runner)
    restarted = AgentOrchestrator(settings, EventStore(store.path))
    assert await restarted.resume_interrupted(client) == 1
    await restarted.drain()
    runner.assert_awaited_once()
    assert "recovered.png" in runner.await_args.args[2]
    assert store.list_interrupted_durable_agent_runs() == []
