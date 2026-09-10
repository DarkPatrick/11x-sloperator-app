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
    review_result_validator,
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


async def test_two_pass_pipeline_uses_analytics_jobs_and_attaches_reviewer(monkeypatch) -> None:
    validator = AsyncMock(return_value=CANDIDATE)
    monkeypatch.setattr("sloperator.experiment_analytics_planner.select_from_jira", validator)
    notification = (
        "<@UONE> [UMN-13002](https://mu--se.atlassian.net/browse/UMN-13002) — "
        "analytics specification is ready. Please check."
    )
    runs = [
        HeadlessAgentRun("claude", "opus", "review-session",
                           "EXPERIMENT_TASK_STARTED: UMN-13002 | UMN-13000 | UMN-13001"),
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
        "experiment-analytics-reviewer",
        "experiment-analytics-preparer",
        "experiment-analytics-reviewer",
    ]
    assert selector.await_count == 1
    validator.assert_awaited_once_with(settings, claimed_task_key=CANDIDATE.task_key)
    assert agent.execute_once.await_args_list[2].kwargs["existing_session_id"] == "review-session"
    assert agent.attach_session.await_args.args[2].session_id == "review-session"


def test_review_notification_rejects_wrong_task() -> None:
    with pytest.raises(InvalidAnalyticsResult):
        normalize_review_notification(
            "Analytics is ready, please check https://mu--se.atlassian.net/browse/UMN-99999",
            CANDIDATE.task_key,
        )


@pytest.mark.parametrize("project", ["Checkout button visibility on low viewports", "Новый экран"])
@pytest.mark.parametrize("review_request", ["посмотрите, пожалуйста", "проверьте, пожалуйста"])
def test_review_notification_accepts_russian_result(project: str, review_request: str) -> None:
    notification = (
        f"<@U02Q5ETBB08> спека аналитики по проекту «{project}» готова и опубликована "
        f"на странице проекта — {review_request}: "
        "<https://mu--se.atlassian.net/browse/UMN-12869|UMN-12869>"
    )
    assert normalize_review_notification(notification, "UMN-12869") == notification
    assert review_result_validator("UMN-12869")(notification)


@pytest.mark.parametrize("text", [
    "Документ готов, посмотрите, пожалуйста",  # Missing analytics subject.
    "Спека аналитики готова",  # Missing review request.
    "Спека аналитики готова, посмотрите https://mu--se.atlassian.net/browse/UMN-99999",
])
def test_review_notification_preserves_validation_guards(text: str) -> None:
    notification = f"{text}: <https://mu--se.atlassian.net/browse/UMN-12869|UMN-12869>"
    assert not review_result_validator("UMN-12869")(notification)


def test_reviewer_starts_and_worker_has_read_only_jira() -> None:
    from sloperator.experiment_analytics_planner import start_prompt

    prompt = start_prompt(CANDIDATE)
    assert "assign the task to that account" in prompt
    assert "transition ID 281" in prompt
    assert "customfield_10312" in prompt
    assert "Jira is read-only for you" in PREPARATION_PROMPT
    assert "transition ID 281" not in PREPARATION_PROMPT


def test_reviewer_prompt_owns_jira_review_metadata_after_comment() -> None:
    prompt = review_prompt("UMN-13024", "UMN-12345")
    assert "Only after the comment is successfully added and verified" in prompt
    assert "target status `In Review`" in prompt
    assert "transition ID `181`" in prompt
    assert "Due date" in prompt
    assert "`duedate`" in prompt
    assert "Jira review update failed" in prompt


def test_review_notification_removes_accidental_outer_code_ticks() -> None:
    notification = (
        f"`<@U123> analytics specification is ready in "
        f"<https://mu--se.atlassian.net/browse/{CANDIDATE.task_key}|{CANDIDATE.task_key}> "
        "— please check it.`"
    )

    assert normalize_review_notification(notification, CANDIDATE.task_key) == notification[1:-1]


@pytest.mark.parametrize("kind", ["design", "analytics"])
def test_all_agent_phases_use_issue_checks_and_scheduler_context(kind: str) -> None:
    from importlib import import_module

    from sloperator.jira_agent_policy import ISSUE_SELECTION_POLICY

    planner = import_module(f"sloperator.experiment_{kind}_planner")
    prompts = [
        planner.start_prompt(CANDIDATE),
        planner.preparation_prompt(CANDIDATE),
        planner.review_prompt(CANDIDATE.task_key, CANDIDATE.epic_key),
    ]
    for prompt in prompts:
        assert ISSUE_SELECTION_POLICY in prompt
        assert "Read its current board configuration and filter" not in prompt
        assert "Derive eligibility from board columns" not in prompt
        assert "Derive status eligibility from the board columns" not in prompt
        assert "AUTOMATED RESPONSE STYLE" in prompt
    for prompt in prompts[:2]:
        assert CANDIDATE.to_json() in prompt
