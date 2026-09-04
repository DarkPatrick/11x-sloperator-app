from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from sloperator.agents import HeadlessAgentRun
from sloperator.config import Settings
from sloperator.experiment_design_planner import (
    NO_OP_RESULT,
    PREPARATION_PROMPT,
    InvalidDesignResult,
    is_preparation_result,
    next_run_at,
    normalize_review_notification,
    parse_preparation_result,
    review_prompt,
    run_once,
    task_key_from_review_result,
)
from sloperator.experiment_design_selector import DesignCandidate

SELECTED = DesignCandidate(
    task_key="UMN-12312",
    epic_key="UMN-12310",
    pitch_key="UMN-12311",
    task_created_at="2026-08-01T10:00:30+00:00",
    pitch_reviewed_at="2026-08-31T10:00:00+00:00",
)


def test_next_run_is_weekdays_at_cyprus_wall_clock_and_preserves_dst() -> None:
    friday_after_run = dt.datetime(2026, 7, 31, 13, 0, tzinfo=dt.UTC)
    saturday_before_run = dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.UTC)

    assert next_run_at(friday_after_run) == dt.datetime(
        2026, 8, 3, 15, 0, tzinfo=ZoneInfo("Asia/Nicosia")
    )
    assert next_run_at(saturday_before_run) == dt.datetime(
        2026, 8, 3, 15, 0, tzinfo=ZoneInfo("Asia/Nicosia")
    )


def test_preparation_prompt_captures_selection_pairing_and_autonomy() -> None:
    assert "[claude]" in PREPARATION_PROMPT
    assert "AUTOMATED RESPONSE STYLE" in PREPARATION_PROMPT
    assert "AUTOMATED ATLASSIAN IDENTITY" in PREPARATION_PROMPT
    assert "pass `--as-bot` on every command" in PREPARATION_PROMPT
    assert "ug-experiment-design-power" in PREPARATION_PROMPT
    assert "cannot\ncommunicate with a human" in PREPARATION_PROMPT
    assert "Project - Hypothesis" in PREPARATION_PROMPT
    assert "closest earlier Pitch task" in PREPARATION_PROMPT
    assert "within 60 seconds" in PREPARATION_PROMPT
    assert "one-to-one pairing" in PREPARATION_PROMPT
    assert "Backlog or To Do column" in PREPARATION_PROMPT
    assert "including\n   `No need`" in PREPARATION_PROMPT
    assert "now minus one calendar month" in PREPARATION_PROMPT
    assert "Do not use issue `updated`" in PREPARATION_PROMPT
    assert "Realistic and Pessimistic" in PREPARATION_PROMPT
    assert "Reach & Impact" in PREPARATION_PROMPT
    assert "Experiment design" in PREPARATION_PROMPT
    assert "stops before launching an agent" in PREPARATION_PROMPT
    assert NO_OP_RESULT not in PREPARATION_PROMPT


def test_review_prompt_requires_independent_correction_and_final_actions() -> None:
    prompt = review_prompt("UMN-12312", "UMN-12310")
    assert "AUTOMATED ATLASSIAN IDENTITY" in prompt
    assert "pass `--as-bot` on every command" in prompt
    assert "Never fall back to personal" in prompt
    assert "Session ownership after publication" in prompt
    assert "own the final corrected solution" in prompt
    assert "responsible\n  author" in prompt
    assert "Never describe yourself as merely a reviewer" in prompt
    assert "no-human constraint above ends" in prompt

    assert "[claude]" in prompt
    assert "ug-experiment-design-power" in prompt
    assert "review/validation mode" in prompt
    assert "open and run the linked sources" in prompt
    assert "still be the oldest eligible" in prompt
    assert "UMN-12312" in prompt
    assert "add one short English comment" in prompt
    assert "board's In Review column" in prompt
    assert "epic assignee" in prompt
    assert "calculation-task assignee" in prompt
    assert "Do not send Slack messages yourself" in prompt


def test_preparation_result_parser_extracts_unambiguous_marker_from_agent_prose() -> None:
    assert parse_preparation_result(NO_OP_RESULT) is None
    assert parse_preparation_result("DESIGN_PREPARED: UMN-12312 | UMN-12310") == (
        "UMN-12312",
        "UMN-12310",
    )
    assert is_preparation_result("DESIGN_PREPARED: UMN-12312 | UMN-12310")
    assert parse_preparation_result(
        "DESIGN_PREPARED: UMN-12520 | UMN-12517\n\n"
        "🧭 Skill: ug-experiment-design-power\n📚 Context: Data warehouse"
    ) == ("UMN-12520", "UMN-12517")
    assert not is_preparation_result("Still working")
    with pytest.raises(InvalidDesignResult):
        parse_preparation_result("Done")
    with pytest.raises(InvalidDesignResult, match="ambiguous"):
        parse_preparation_result("DESIGN_PREPARED: UMN-1 | UMN-2\nDESIGN_PREPARED: UMN-3 | UMN-4")


async def test_no_candidate_finishes_without_slack_or_second_agent() -> None:
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    agent = SimpleNamespace(
        execute_once=AsyncMock(
            return_value=HeadlessAgentRun(
                provider="claude",
                model="opus",
                session_id="prepare-session",
                text=NO_OP_RESULT,
            )
        ),
        attach_session=AsyncMock(),
    )
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )

    selector = AsyncMock(return_value=None)

    assert await run_once(client, agent, settings, selector) is None
    assert agent.execute_once.await_count == 0
    selector.assert_awaited_once_with(settings)
    client.chat_postMessage.assert_not_awaited()
    agent.attach_session.assert_not_awaited()


async def test_preparation_then_independent_review_publishes_once() -> None:
    notification = (
        "<@UONE> <@UTWO> "
        "[UMN-12312](https://mu--se.atlassian.net/browse/UMN-12312) — "
        "Reach & Impact and Experiment design are ready. Please check."
    )
    runs = [
        HeadlessAgentRun(
            provider="claude",
            model="opus",
            session_id="prepare-session",
            text="DESIGN_PREPARED: UMN-12312 | UMN-12310",
        ),
        HeadlessAgentRun(
            provider="claude",
            model="opus",
            session_id="review-session",
            text=notification,
        ),
    ]
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"channel": "CDESIGN", "ts": "100.1"})
    )
    agent = SimpleNamespace(
        execute_once=AsyncMock(side_effect=runs),
        attach_session=AsyncMock(),
    )
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        experiment_design_channel="CDESIGN",
    )

    selector = AsyncMock(return_value=SELECTED)

    assert await run_once(client, agent, settings, selector) == notification
    assert agent.execute_once.await_count == 2
    first, second = agent.execute_once.await_args_list
    assert first.kwargs["job_name"] == "experiment-design-preparer"
    assert second.kwargs["job_name"] == "experiment-design-reviewer"
    assert "UMN-12312" in second.args[0]
    assert "UMN-12311" in first.args[0]
    assert selector.await_count == 2
    client.chat_postMessage.assert_awaited_once_with(
        channel="CDESIGN",
        markdown_text=notification,
        unfurl_links=False,
        unfurl_media=False,
    )
    agent.attach_session.assert_awaited_once()
    assert agent.attach_session.await_args.args[2].session_id == "review-session"


def test_review_notification_rejects_wrong_task_or_verbose_output() -> None:
    with pytest.raises(InvalidDesignResult):
        normalize_review_notification(
            "Please check https://mu--se.atlassian.net/browse/UMN-99999", "UMN-12312"
        )
    assert (
        normalize_review_notification(
            "Internal note\nPlease check "
            "https://mu--se.atlassian.net/browse/UMN-12312\nAudit: done",
            "UMN-12312",
        )
        == "Please check https://mu--se.atlassian.net/browse/UMN-12312"
    )


async def test_changed_selection_stops_before_review() -> None:
    prepared = HeadlessAgentRun(
        provider="claude",
        model="opus",
        session_id="prepare-session",
        text="DESIGN_PREPARED: UMN-12312 | UMN-12310",
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    agent = SimpleNamespace(
        execute_once=AsyncMock(return_value=prepared),
        attach_session=AsyncMock(),
    )
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    selector = AsyncMock(side_effect=[SELECTED, None])

    with pytest.raises(InvalidDesignResult, match="changed before review"):
        await run_once(client, agent, settings, selector)

    assert agent.execute_once.await_count == 1
    client.chat_postMessage.assert_not_awaited()


def test_recovered_review_result_extracts_the_linked_task() -> None:
    text = (
        "<@UONE> [UMN-12312](https://mu--se.atlassian.net/browse/UMN-12312) — "
        "design is ready. Please check."
    )

    assert task_key_from_review_result(text) == "UMN-12312"
