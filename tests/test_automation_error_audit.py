from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from sloperator.agents import HeadlessAgentRun
from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.automation_error_audit import (
    AUDIT_PROMPT,
    NO_ERRORS,
    PROJECT_ROOT,
    REPORT_PREFIX,
    TIMEOUT_SECONDS,
    is_final_result,
    next_run_at,
    run_once,
)
from sloperator.config import Settings


def test_prompt_is_claude_read_only_and_uses_automated_response_style() -> None:
    assert AUDIT_PROMPT.startswith("[claude:opus]")
    assert AUTOMATED_RESPONSE_STYLE in AUDIT_PROMPT
    assert "preceding 24 hours" in AUDIT_PROMPT
    assert "Slack-triggered agent runs" in AUDIT_PROMPT
    assert "expected operational state, not an automation failure" in AUDIT_PROMPT
    assert "Do not report it" in AUDIT_PROMPT
    assert "Do not edit or create files" in AUDIT_PROMPT
    assert "Do not repair anything" in AUDIT_PROMPT
    assert NO_ERRORS in AUDIT_PROMPT
    assert REPORT_PREFIX in AUDIT_PROMPT


def test_next_run_is_daily_at_14_cyprus_including_weekends() -> None:
    friday_after = dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.UTC)
    saturday_before = dt.datetime(2026, 8, 1, 8, 0, tzinfo=dt.UTC)

    assert next_run_at(friday_after) == dt.datetime(
        2026, 8, 1, 14, 0, tzinfo=ZoneInfo("Asia/Nicosia")
    )
    assert next_run_at(saturday_before) == dt.datetime(
        2026, 8, 1, 14, 0, tzinfo=ZoneInfo("Asia/Nicosia")
    )


def test_only_contract_results_are_final() -> None:
    assert is_final_result(NO_ERRORS)
    assert is_final_result(f"{REPORT_PREFIX} cron x failed")
    assert not is_final_result("I am checking the logs now")


async def test_clean_audit_stays_silent() -> None:
    agent = SimpleNamespace(
        execute_once=AsyncMock(
            return_value=HeadlessAgentRun("claude", "opus", "session", NO_ERRORS)
        )
    )
    client = SimpleNamespace(
        conversations_open=AsyncMock(),
        chat_postMessage=AsyncMock(),
    )
    settings = Settings("UOWNER", "xoxb-test", "xapp-test")

    assert await run_once(client, agent, settings) is None
    agent.execute_once.assert_awaited_once_with(
        AUDIT_PROMPT,
        TIMEOUT_SECONDS,
        job_name="automation-error-audit",
        workspace=PROJECT_ROOT,
        accept_result=is_final_result,
    )
    client.conversations_open.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()


async def test_failure_report_is_sent_to_owner_dm() -> None:
    report = f"{REPORT_PREFIX} cron x failed; cause established."
    agent = SimpleNamespace(
        execute_once=AsyncMock(return_value=HeadlessAgentRun("claude", "opus", "session", report))
    )
    client = SimpleNamespace(
        conversations_open=AsyncMock(return_value={"channel": {"id": "DOWNER"}}),
        chat_postMessage=AsyncMock(),
    )
    settings = Settings("UOWNER", "xoxb-test", "xapp-test")

    assert await run_once(client, agent, settings) == report
    client.conversations_open.assert_awaited_once_with(users="UOWNER")
    client.chat_postMessage.assert_awaited_once_with(
        channel="DOWNER",
        markdown_text=report,
        unfurl_links=False,
        unfurl_media=False,
    )
