from __future__ import annotations

from typing import Any

import pytest

from sloperator.config import Settings
from sloperator.experiment_design_selector import JiraRestReader, SelectionError
from sloperator.team_channel import RETENTION_DEBT_LABEL, channel_for_task, first_task_key

DEFAULT = "C07A9FDQ14P"
RETENTION = "C0BT7HSVACD"
ISSUES: dict[str, dict[str, Any]] = {
    "UMN-1": {"issuetype": {"name": "Эпик"}, "labels": [RETENTION_DEBT_LABEL]},
    "UMN-2": {"issuetype": {"name": "Задача"}, "labels": [], "parent": {"key": "UMN-1"}},
    "UMN-3": {"issuetype": {"name": "Epic"}, "labels": ["other"]},
    "UMN-4": {"issuetype": {"name": "Task"}, "labels": [RETENTION_DEBT_LABEL],
              "parent": {"key": "UMN-3"}},
    "UMN-5": {"issuetype": {"name": "Task"}, "labels": []},
}


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "jira_username": "bot@example.com", "jira_api_token": "token",
        "retention_debt_channel": RETENTION, **overrides,
    }
    return Settings(slack_user_id="U1234567890", bot_token="test", app_token="test", **values)


@pytest.fixture(autouse=True)
def fake_jira(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get(self, path: str, params=None):
        key = path.rsplit("/", 1)[1]
        if key == "UMN-9":
            raise SelectionError("Jira GET failed with HTTP 503")
        return {"fields": ISSUES[key]}

    monkeypatch.setattr(JiraRestReader, "_get", get)


@pytest.mark.parametrize(
    ("task_key", "expected"),
    [
        ("UMN-1", RETENTION),  # the epic itself
        ("UMN-2", RETENTION),  # task under a labelled epic
        ("UMN-3", DEFAULT),
        ("UMN-4", DEFAULT),  # only the epic's label counts
        ("UMN-5", DEFAULT),  # no parent epic
        ("UMN-9", DEFAULT),  # Jira failure keeps the post in the default channel
        (None, DEFAULT),
    ],
)
async def test_channel_follows_epic_label(task_key: str | None, expected: str) -> None:
    assert await channel_for_task(settings(), task_key, DEFAULT) == expected


async def test_routing_disabled_without_channel_or_credentials() -> None:
    assert await channel_for_task(settings(retention_debt_channel=""), "UMN-2", DEFAULT) == DEFAULT
    no_jira = Settings(slack_user_id="U1234567890", bot_token="test", app_token="test")
    assert await channel_for_task(no_jira, "UMN-2", DEFAULT) == DEFAULT


def test_first_task_key_reads_failure_lines() -> None:
    assert first_task_key("Experiment design automation failed: UMN-2: no page") == "UMN-2"
    assert first_task_key("Experiment design automation failed: board 401") is None
