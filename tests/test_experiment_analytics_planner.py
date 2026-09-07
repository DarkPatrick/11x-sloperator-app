from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.agents import HeadlessAgentRun
from sloperator.config import Settings
from sloperator.experiment_analytics_planner import (
    PREPARATION_PROMPT,
    InvalidAnalyticsResult,
    normalize_review_notification,
    review_prompt,
    run_once,
)
from sloperator.experiment_design_selector import DesignCandidate

CANDIDATE = DesignCandidate(
    task_key="UMN-13002",
    epic_key="UMN-13000",
    pitch_key="UMN-13001",
    task_created_at="2026-09-03T10:00:02+00:00",
    pitch_reviewed_at="2026-09-04T09:00:00+00:00",
)


def test_prompts_use_analytics_skill_service_accounts_and_owner_session() -> None:
    prompt = review_prompt(CANDIDATE.task_key, CANDIDATE.epic_key)
    assert "ug-analytics-spec-writer" in PREPARATION_PROMPT
    assert "AUTOMATED RESPONSE STYLE" in PREPARATION_PROMPT
    assert "AUTOMATED ATLASSIAN IDENTITY" in PREPARATION_PROMPT
    assert "`Аналитика`" in PREPARATION_PROMPT
    assert "responsible author" in prompt
    assert "Never describe yourself as merely a reviewer" in prompt


async def test_two_pass_pipeline_uses_analytics_jobs_and_attaches_reviewer() -> None:
    notification = (
        "<@UONE> [UMN-13002](https://mu--se.atlassian.net/browse/UMN-13002) — "
        "analytics specification is ready. Please check."
    )
    runs = [
        HeadlessAgentRun(
            "claude",
            "opus",
            "prepare-session",
            "ANALYTICS_PREPARED: UMN-13002 | UMN-13000",
        ),
        HeadlessAgentRun("claude", "opus", "review-session", notification),
    ]
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"channel": "CANALYTICS", "ts": "100.1"})
    )
    agent = SimpleNamespace(execute_once=AsyncMock(side_effect=runs), attach_session=AsyncMock())
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        experiment_analytics_channel="CANALYTICS",
    )
    selector = AsyncMock(return_value=CANDIDATE)

    assert await run_once(client, agent, settings, selector) == notification
    assert [call.kwargs["job_name"] for call in agent.execute_once.await_args_list] == [
        "experiment-analytics-preparer",
        "experiment-analytics-reviewer",
    ]
    assert selector.await_count == 2
    assert agent.attach_session.await_args.args[2].session_id == "review-session"


def test_review_notification_rejects_wrong_task() -> None:
    with pytest.raises(InvalidAnalyticsResult):
        normalize_review_notification(
            "Analytics is ready, please check https://mu--se.atlassian.net/browse/UMN-99999",
            CANDIDATE.task_key,
        )


def test_preparer_prompt_owns_jira_start_metadata() -> None:
    assert "assign the task to that account" in PREPARATION_PROMPT
    assert "In Progress column" in PREPARATION_PROMPT
    assert "Start date" in PREPARATION_PROMPT
    assert "never guess a customfield id" in PREPARATION_PROMPT
    assert "Jira start update failed" in PREPARATION_PROMPT


def test_reviewer_prompt_owns_jira_review_metadata_after_comment() -> None:
    prompt = review_prompt("UMN-13024", "UMN-12345")
    assert "Only after the comment is successfully added and verified" in prompt
    assert "In Review column" in prompt
    assert "Due date" in prompt
    assert "Jira review update failed" in prompt


def test_review_notification_removes_accidental_outer_code_ticks() -> None:
    notification = (
        f"`<@U123> analytics specification is ready in "
        f"<https://mu--se.atlassian.net/browse/{CANDIDATE.task_key}|{CANDIDATE.task_key}> "
        "— please check it.`"
    )

    assert normalize_review_notification(notification, CANDIDATE.task_key) == notification[1:-1]
