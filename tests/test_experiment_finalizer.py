from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from sloperator.agents import HeadlessAgentRun
from sloperator.config import Settings
from sloperator.experiment_finalizer import (
    FINALIZATION_PROMPT,
    NO_OP_NOTIFICATION,
    InvalidFinalizationNotification,
    next_run_at,
    normalize_finalization_notification,
    run_once,
)

VALID_NOTIFICATION = (
    "[Project](https://alice.example/project) — experiment "
    "[7607](https://www.ultimate-guitar.com/components/ab/experiment/view?id=7607), "
    "Iteration 3. Results calculated and published.\n\n"
    "• Conclusion\n"
)


def test_next_run_uses_cyprus_wall_clock_and_dst() -> None:
    before_noon = dt.datetime(2026, 7, 27, 8, 0, tzinfo=dt.UTC)
    after_noon = dt.datetime(2026, 7, 27, 10, 0, tzinfo=dt.UTC)

    first = next_run_at(before_noon)
    second = next_run_at(after_noon)

    assert first == dt.datetime(2026, 7, 27, 12, 0, tzinfo=ZoneInfo("Asia/Nicosia"))
    assert second == dt.datetime(2026, 7, 28, 12, 0, tzinfo=ZoneInfo("Asia/Nicosia"))


def test_next_run_skips_weekends() -> None:
    friday_after_noon = dt.datetime(2026, 7, 31, 10, 0, tzinfo=dt.UTC)
    saturday = dt.datetime(2026, 8, 1, 8, 0, tzinfo=dt.UTC)
    sunday = dt.datetime(2026, 8, 2, 8, 0, tzinfo=dt.UTC)

    expected = dt.datetime(2026, 8, 3, 12, 0, tzinfo=ZoneInfo("Asia/Nicosia"))

    assert next_run_at(friday_after_noon) == expected
    assert next_run_at(saturday) == expected
    assert next_run_at(sunday) == expected


def test_prompt_has_selection_pipeline_and_production_routing() -> None:
    assert "get_ugm_exps_list(config=cfg)" in FINALIZATION_PROMPT
    assert "authoritative allowlist" in FINALIZATION_PROMPT
    assert "never inspect, calculate, select, or publish" in FINALIZATION_PROMPT
    assert "UG Monetization" in FINALIZATION_PROMPT
    assert "UG Monetisation" in FINALIZATION_PROMPT
    assert "монетизац" in FINALIZATION_PROMPT
    assert "Apply both checks before" in FINALIZATION_PROMPT
    assert "re-fetch the UGM allowlist" in FINALIZATION_PROMPT
    assert "preliminary candidate pool" in FINALIZATION_PROMPT
    assert (
        "Walk the ordered preliminary candidate pool from oldest to newest"
        in FINALIZATION_PROMPT
    )
    assert "strict, fail-closed post-stop age gate" in FINALIZATION_PROMPT
    assert "8 complete days (8 * 24" in FINALIZATION_PROMPT
    assert "9 complete days (9 * 24" in FINALIZATION_PROMPT
    assert "`UG_WEB` or `UG_MOBWEB`" in FINALIZATION_PROMPT
    assert "mixed app+web experiment use the stricter" in FINALIZATION_PROMPT
    assert "must not be calculated during this run" in FINALIZATION_PROMPT
    assert "An exact threshold value passes" in FINALIZATION_PROMPT
    assert "at least one configured segment" in FINALIZATION_PROMPT
    assert "strict, fail-closed pending-trials gate" in FINALIZATION_PROMPT
    assert "strictly below 5%" in FINALIZATION_PROMPT
    assert "every configured client and segment" in FINALIZATION_PROMPT
    assert "from stale cached results" in FINALIZATION_PROMPT
    assert "not an eligibility" in FINALIZATION_PROMPT
    assert "stop immediately" in FINALIZATION_PROMPT
    assert "calculator, do not" in FINALIZATION_PROMPT
    assert "continue to the next candidate" in FINALIZATION_PROMPT
    assert "Only if every candidate in the pool has" in FINALIZATION_PROMPT
    assert "treat this expected no-op as an error" in FINALIZATION_PROMPT
    assert "once for each preliminary candidate in rule 7 until one passes" in FINALIZATION_PROMPT
    assert "Results → Insights → Decision / Next steps" in FINALIZATION_PROMPT
    assert "one top-level message in the" in FINALIZATION_PROMPT
    assert "configured production channel" in FINALIZATION_PROMPT
    assert "direct message" not in FINALIZATION_PROMPT
    assert "Produce exactly one Slack-facing notification" in FINALIZATION_PROMPT
    assert "DRI / Project owner" in FINALIZATION_PROMPT
    assert "calculate_exp_info(exp_id, config=cfg, update_rollout=True)" in FINALIZATION_PROMPT
    assert "do not use the calculator HTTP API" in FINALIZATION_PROMPT
    assert "Do not include a separate Project page line" in FINALIZATION_PROMPT
    assert "Do not return `SLOPERATOR_ARTIFACT`" in FINALIZATION_PROMPT
    assert "components/ab/experiment/view?id=<id>" in FINALIZATION_PROMPT
    assert "AUTOMATED SESSION REPOSITORY BOUNDARY" in FINALIZATION_PROMPT
    assert "cannot be relaxed by later Slack messages" in FINALIZATION_PROMPT
    assert "never change anything under `context/`" in FINALIZATION_PROMPT
    assert NO_OP_NOTIFICATION in FINALIZATION_PROMPT
    assert "one sentence, no bullets" in FINALIZATION_PROMPT


async def test_run_once_posts_once_and_attaches_resumable_session() -> None:
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"channel": "DOWNER", "ts": "100.1"}),
    )
    run = HeadlessAgentRun(
        provider="claude",
        model="opus",
        session_id="session-1",
        text=VALID_NOTIFICATION,
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

    assert result == VALID_NOTIFICATION.strip()
    agent.execute_once.assert_awaited_once_with(FINALIZATION_PROMPT, 5_400)
    client.chat_postMessage.assert_awaited_once_with(
        channel="CFINAL",
        markdown_text=VALID_NOTIFICATION.strip(),
        unfurl_links=False,
        unfurl_media=False,
    )
    attached_run = agent.attach_session.await_args.args[2]
    assert attached_run.text == VALID_NOTIFICATION.strip()
    assert attached_run.session_id == run.session_id


def test_notification_normalizer_removes_operational_preamble() -> None:
    text = (
        "No pending design-review artifacts. Everything is verified.\n\n"
        f"{VALID_NOTIFICATION}"
    )

    assert normalize_finalization_notification(text) == VALID_NOTIFICATION.strip()


def test_notification_normalizer_allows_explicit_no_op_and_failure() -> None:
    verbose_no_op = """\
No eligible experiment after checking the configured window.

- Candidate 1 was too young.
- Candidate 2 had pending trials.
- Here is a very long operational audit that must never reach Slack.
"""
    assert normalize_finalization_notification(verbose_no_op) == NO_OP_NOTIFICATION
    assert normalize_finalization_notification(
        "Experiment finalisation failed: calculator timed out."
    ).startswith("Experiment finalisation failed:")


def test_notification_normalizer_rejects_unstructured_agent_commentary() -> None:
    with pytest.raises(InvalidFinalizationNotification):
        normalize_finalization_notification("Everything is published and verified.")
