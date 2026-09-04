from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pytest

from sloperator.experiment_design_selector import SelectionError, select_candidate


def raw_issue(
    key: str,
    summary: str,
    status_id: str,
    created: str,
    *,
    issue_type: str = "Task",
    parent: str | None = None,
    components: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"id": status_id},
            "issuetype": {"name": issue_type},
            "components": [{"name": name} for name in components],
            "created": created,
            "parent": {"key": parent} if parent else None,
        },
    }


CONFIGURATION = {
    "filter": {"id": "12345"},
    "columnConfig": {
        "columns": [
            {"name": "Бэклог", "statuses": [{"id": "1"}]},
            {"name": "To Do", "statuses": [{"id": "2"}]},
            {"name": "In Progress", "statuses": [{"id": "3"}]},
            {"name": "In Review", "statuses": [{"id": "4"}]},
            {"name": "Done", "statuses": [{"id": "5"}, {"id": "6"}]},
        ]
    },
}


@dataclass
class FakeJira:
    epics: list[dict[str, Any]]
    children: list[dict[str, Any]]
    changelogs: dict[str, list[dict[str, Any]]]
    requested_changelogs: list[str] = field(default_factory=list)

    async def board_configuration(self, board_id: int) -> dict[str, Any]:
        assert board_id == 175
        return CONFIGURATION

    async def board_issues(self, board_id: int, filter_id: str) -> list[dict[str, Any]]:
        assert board_id == 175
        assert filter_id == "12345"
        return self.epics

    async def child_issues(self, epic_keys: list[str]) -> list[dict[str, Any]]:
        assert set(epic_keys) == {
            issue["key"]
            for issue in self.epics
            if issue["fields"]["status"]["id"] == "3"
            and {item["name"] for item in issue["fields"]["components"]} == {"Project - Hypothesis"}
        }
        return self.children

    async def issue_changelog(self, issue_key: str) -> list[dict[str, Any]]:
        self.requested_changelogs.append(issue_key)
        return self.changelogs.get(issue_key, [])


def transition(created: str, status_id: str) -> dict[str, Any]:
    return {
        "created": created,
        "items": [{"fieldId": "status", "field": "status", "to": status_id}],
    }


NOW = dt.datetime(2026, 9, 2, 12, tzinfo=dt.UTC)


async def test_selects_oldest_eligible_pair_deterministically() -> None:
    jira = FakeJira(
        epics=[
            raw_issue(
                "UMN-100",
                "Eligible epic",
                "3",
                "2026-07-01T00:00:00Z",
                issue_type="Эпик",
                components=("Project - Hypothesis",),
            ),
            raw_issue(
                "UMN-999",
                "Wrong component",
                "3",
                "2026-07-01T00:00:00Z",
                issue_type="Epic",
                components=("Other",),
            ),
        ],
        children=[
            raw_issue(
                "UMN-101",
                "Проектирование и Питч — first",
                "5",
                "2026-08-01T10:00:00Z",
                parent="UMN-100",
            ),
            raw_issue(
                "UMN-102",
                "Расчёт сверху и план тестирования — first",
                "1",
                "2026-08-01T10:00:30Z",
                parent="UMN-100",
            ),
            raw_issue(
                "UMN-103",
                "Проектирование и Питч — second",
                "4",
                "2026-08-02T10:00:00Z",
                parent="UMN-100",
            ),
            raw_issue(
                "UMN-104",
                "Расчет сверху и план тестирования — second",
                "2",
                "2026-08-02T10:00:20Z",
                parent="UMN-100",
            ),
        ],
        changelogs={
            "UMN-101": [transition("2026-08-20T12:00:00Z", "5")],
            "UMN-103": [transition("2026-08-21T12:00:00Z", "4")],
        },
    )

    candidate = await select_candidate(jira, now=NOW)

    assert candidate is not None
    assert (candidate.task_key, candidate.pitch_key, candidate.epic_key) == (
        "UMN-102",
        "UMN-101",
        "UMN-100",
    )
    assert jira.requested_changelogs == ["UMN-101", "UMN-103"]


async def test_can_select_analytics_task_with_the_same_pairing_rules() -> None:
    jira = FakeJira(
        epics=[
            raw_issue(
                "UMN-200",
                "Analytics epic",
                "3",
                "2026-08-01T00:00:00Z",
                issue_type="Epic",
                components=("Project - Hypothesis",),
            )
        ],
        children=[
            raw_issue(
                "UMN-201",
                "Проектирование и Питч — iteration 2",
                "5",
                "2026-08-20T10:00:00Z",
                parent="UMN-200",
            ),
            raw_issue(
                "UMN-202",
                "Аналитика — iteration 2",
                "1",
                "2026-08-20T10:00:40Z",
                parent="UMN-200",
            ),
        ],
        changelogs={"UMN-201": [transition("2026-08-25T12:00:00Z", "5")]},
    )

    candidate = await select_candidate(jira, now=NOW, task_title="Аналитика")

    assert candidate is not None
    assert (candidate.task_key, candidate.pitch_key, candidate.epic_key) == (
        "UMN-202",
        "UMN-201",
        "UMN-200",
    )


async def test_excludes_old_transition_wrong_columns_and_pair_outside_window() -> None:
    jira = FakeJira(
        epics=[
            raw_issue(
                "UMN-100",
                "Epic",
                "3",
                "2026-07-01T00:00:00Z",
                issue_type="Epic",
                components=("Project - Hypothesis",),
            )
        ],
        children=[
            raw_issue(
                "UMN-101", "Проектирование и Питч", "5", "2026-07-01T00:00:00Z", parent="UMN-100"
            ),
            raw_issue(
                "UMN-102",
                "Расчет сверху и план тестирования",
                "1",
                "2026-07-01T00:01:01Z",
                parent="UMN-100",
            ),
            raw_issue(
                "UMN-103", "Проектирование и Питч", "5", "2026-08-01T00:00:00Z", parent="UMN-100"
            ),
            raw_issue(
                "UMN-104",
                "Расчет сверху и план тестирования",
                "1",
                "2026-08-01T00:00:20Z",
                parent="UMN-100",
            ),
        ],
        changelogs={"UMN-103": [transition("2026-07-31T12:00:00Z", "5")]},
    )

    assert await select_candidate(jira, now=NOW) is None


async def test_ambiguous_equidistant_pitch_pair_is_excluded() -> None:
    jira = FakeJira(
        epics=[
            raw_issue(
                "UMN-100",
                "Epic",
                "3",
                "2026-08-01T00:00:00Z",
                issue_type="Epic",
                components=("Project - Hypothesis",),
            )
        ],
        children=[
            raw_issue(
                "UMN-101", "Проектирование и Питч A", "5", "2026-08-01T10:00:00Z", parent="UMN-100"
            ),
            raw_issue(
                "UMN-102", "Проектирование и Питч B", "5", "2026-08-01T10:00:00Z", parent="UMN-100"
            ),
            raw_issue(
                "UMN-103",
                "Расчет сверху и план тестирования",
                "1",
                "2026-08-01T10:00:30Z",
                parent="UMN-100",
            ),
        ],
        changelogs={},
    )

    assert await select_candidate(jira, now=NOW) is None
    assert jira.requested_changelogs == []


async def test_missing_required_board_column_fails_closed() -> None:
    jira = FakeJira([], [], {})

    async def incomplete_configuration(board_id: int) -> dict[str, Any]:
        return {"filter": {"id": "12345"}, "columnConfig": {"columns": []}}

    jira.board_configuration = incomplete_configuration  # type: ignore[method-assign]
    with pytest.raises(SelectionError, match="columns"):
        await select_candidate(jira, now=NOW)
