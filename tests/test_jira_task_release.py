from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

from sloperator.config import Settings
from sloperator.jira_task_release import SERVICE_ACCOUNT_ID, find_bot_start, release_task

NOW = dt.datetime(2026, 11, 9, 14, 0, tzinfo=dt.UTC)
BOT = {"accountId": SERVICE_ACCOUNT_ID}
HUMAN = {"accountId": "557058:human"}


def status(created: str, author: dict, src: str, dst: str) -> dict:
    return {
        "created": created, "author": author,
        "items": [{"field": "status", "fieldId": "status", "fromString": src, "toString": dst}],
    }


def test_finds_the_bot_start_and_what_it_changed() -> None:
    histories = [
        {"created": "2026-11-01T22:08:23.000+0200", "author": HUMAN,
         "items": [{"field": "assignee", "from": None, "to": SERVICE_ACCOUNT_ID}]},
        {"created": "2026-11-09T15:00:40.000+0200", "author": BOT,
         "items": [{"field": "Start date", "fieldId": "customfield_10312", "from": None,
                    "to": "2026-11-09"}]},
        status("2026-11-09T15:01:09.000+0200", BOT, "To Do", "In Progress"),
    ]
    start = find_bot_start(histories, NOW)
    assert start is not None
    assert start.previous_status == "To Do"
    assert start.start_date_set
    # The assignee was set by a human a week earlier, so it is not the bot's to undo.
    assert not start.assignee_changed


def test_leaves_human_moves_finished_and_old_starts_alone() -> None:
    human = [status("2026-11-09T15:01:09.000+0200", HUMAN, "To Do", "In Progress")]
    reviewed = [
        status("2026-11-09T15:01:09.000+0200", BOT, "To Do", "In Progress"),
        status("2026-11-09T15:30:00.000+0200", BOT, "In Progress", "In Review"),
    ]
    old = [status("2026-11-06T15:01:09.000+0200", BOT, "To Do", "In Progress")]
    assert find_bot_start(human, NOW) is None
    assert find_bot_start(reviewed, NOW) is None
    assert find_bot_start(old, NOW) is None


async def test_release_goes_through_backlog_when_there_is_no_direct_path(monkeypatch) -> None:
    from sloperator.experiment_design_selector import JiraRestReader

    monkeypatch.setattr(JiraRestReader, "issue_changelog", AsyncMock(return_value=[
        status("2026-11-09T15:01:09.000+0200", BOT, "To Do", "In Progress"),
    ]))
    outputs = iter([
        "  231: Backlog  -> Backlog\n  281: In Progress  -> In Progress\n",
        "transitioned",
        "  231: Backlog  -> Backlog\n  241: Start working  -> To Do\n",
        "transitioned",
    ])
    calls: list[tuple[str, ...]] = []

    async def helper(_settings, *args):
        calls.append(args)
        return next(outputs)

    monkeypatch.setattr("sloperator.jira_task_release._helper", helper)
    settings = Settings(
        slack_user_id="U", bot_token="x", app_token="y", jira_username="u", jira_api_token="t"
    )

    assert await release_task(settings, "UMN-14144", now=NOW) == "To Do"
    assert calls == [
        ("transitions", "UMN-14144"),
        ("transition", "UMN-14144", "--id", "231"),
        ("transitions", "UMN-14144"),
        ("transition", "UMN-14144", "--id", "241"),
    ]


async def test_release_failure_never_blocks_the_post(monkeypatch) -> None:
    from sloperator.experiment_design_selector import JiraRestReader

    monkeypatch.setattr(
        JiraRestReader, "issue_changelog", AsyncMock(side_effect=RuntimeError("HTTP 500"))
    )
    settings = Settings(
        slack_user_id="U", bot_token="x", app_token="y", jira_username="u", jira_api_token="t"
    )
    assert await release_task(settings, "UMN-1", now=NOW) is None
