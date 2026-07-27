from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from sloperator.agents import SubmitResult
from sloperator.config import Settings
from sloperator.experiment_finalizer import FINALIZATION_PROMPT, next_run_at, run_once


def test_next_run_uses_cyprus_wall_clock_and_dst() -> None:
    before_noon = dt.datetime(2026, 7, 27, 8, 0, tzinfo=dt.UTC)
    after_noon = dt.datetime(2026, 7, 27, 10, 0, tzinfo=dt.UTC)

    first = next_run_at(before_noon)
    second = next_run_at(after_noon)

    assert first == dt.datetime(2026, 7, 27, 12, 0, tzinfo=ZoneInfo("Asia/Nicosia"))
    assert second == dt.datetime(2026, 7, 28, 12, 0, tzinfo=ZoneInfo("Asia/Nicosia"))


def test_prompt_has_selection_pipeline_and_test_routing() -> None:
    assert "oldest by actual end timestamp" in FINALIZATION_PROMPT
    assert "at least one configured segment" in FINALIZATION_PROMPT
    assert "Results → Insights → Decision / Next steps" in FINALIZATION_PROMPT
    assert "Do not post to `ug-monetization-pvt`" in FINALIZATION_PROMPT
    assert "DRI / Project owner" in FINALIZATION_PROMPT
    assert "calculate_exp_info(exp_id, config=cfg, update_rollout=True)" in FINALIZATION_PROMPT
    assert "do not use the calculator HTTP API" in FINALIZATION_PROMPT


async def test_run_once_starts_private_agent_thread() -> None:
    client = SimpleNamespace(
        conversations_open=AsyncMock(return_value={"channel": {"id": "D123"}}),
        chat_postMessage=AsyncMock(return_value={"ts": "100.1"}),
    )
    agent = SimpleNamespace(submit=AsyncMock(return_value=SubmitResult.QUEUED))
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )

    result = await run_once(client, agent, settings)

    assert result is SubmitResult.QUEUED
    agent.submit.assert_awaited_once_with(
        client,
        channel_id="D123",
        message_ts="100.1",
        thread_ts="100.1",
        text=FINALIZATION_PROMPT,
        show_status=True,
        timeout_seconds=5_400,
    )
