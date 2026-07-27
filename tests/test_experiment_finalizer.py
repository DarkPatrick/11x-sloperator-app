from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from sloperator.agents import HeadlessAgentRun
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
    assert "`ug-monetization-pvt`" in FINALIZATION_PROMPT
    assert "DRI / Project owner" in FINALIZATION_PROMPT
    assert "calculate_exp_info(exp_id, config=cfg, update_rollout=True)" in FINALIZATION_PROMPT
    assert "do not use the calculator HTTP API" in FINALIZATION_PROMPT
    assert "Do not include a separate Project page line" in FINALIZATION_PROMPT
    assert "Do not return `SLOPERATOR_ARTIFACT`" in FINALIZATION_PROMPT
    assert "components/ab/experiment/view?id=<id>" in FINALIZATION_PROMPT


async def test_run_once_posts_once_and_attaches_resumable_session() -> None:
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "100.1"}),
    )
    run = HeadlessAgentRun(
        provider="claude",
        model="opus",
        session_id="session-1",
        text="Final result",
    )
    agent = SimpleNamespace(
        execute_once=AsyncMock(return_value=run),
        attach_session=AsyncMock(),
    )
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        experiment_finalizer_channel="CFINAL",
    )

    result = await run_once(client, agent, settings)

    assert result == "Final result"
    agent.execute_once.assert_awaited_once_with(FINALIZATION_PROMPT, 5_400)
    client.chat_postMessage.assert_awaited_once_with(
        channel="CFINAL",
        markdown_text="Final result",
        unfurl_links=False,
        unfurl_media=False,
    )
    agent.attach_session.assert_awaited_once_with("CFINAL", "100.1", run)
