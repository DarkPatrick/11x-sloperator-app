from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pytest

from sloperator.experiment_design_selector import (
    JiraRestReader,
    SelectionError,
    resolve_project_page_id,
    select_candidate,
)


async def test_epic_page_links_reads_confluence_page_id_from_remote_global_id(
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock

    jira = JiraRestReader("https://jira.example", "user", "token")
    jira._get = AsyncMock(return_value={"fields": {"description": None}})  # type: ignore[method-assign]

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return [{
                "globalId": "appId=abc&pageId=822915848",
                "application": {"type": "com.atlassian.confluence"},
                "relationship": "mentioned in",
                "object": {"title": "Project page"},
            }]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, _url):
            return Response()

    monkeypatch.setattr(
        "sloperator.experiment_design_selector.ClientSession",
        lambda **_kwargs: Session(),
    )

    assert await jira.epic_page_links("UMN-12542") == [
        "https://alice.mu.se/pages/viewpage.action?pageId=822915848"
    ]


async def test_project_page_requires_one_link_on_epic() -> None:
    from unittest.mock import AsyncMock

    jira = AsyncMock()
    jira.epic_page_links.return_value = [
        "https://alice.mu.se/pages/viewpage.action?pageId=838613487",
        "https://alice.mu.se/spaces/CRO/pages/838613487/project",
    ]
    assert await resolve_project_page_id(jira, "UMN-13405") == "838613487"
    jira.epic_page_links.assert_awaited_once_with("UMN-13405")

    jira.epic_page_links.return_value = []
    with pytest.raises(SelectionError, match="no Confluence project-page link"):
        await resolve_project_page_id(jira, "UMN-13405")



def header(epic_key: str) -> str:
    return (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>Naming</p>'
        "</ac:rich-text-body></ac:structured-macro>"
        '<table><tbody><tr><th>Status</th><td><ac:structured-macro ac:name="jira">'
        f'<ac:parameter ac:name="key">{epic_key}</ac:parameter></ac:structured-macro>'
        "</td></tr></tbody></table><table><tr><td>UMN-13405</td></tr></table>"
    )


async def test_project_page_with_several_links_uses_epic_in_header_table() -> None:
    from unittest.mock import AsyncMock

    jira = AsyncMock()
    jira.epic_page_links.return_value = [
        "https://alice.mu.se/pages/viewpage.action?pageId=761947509",
        "https://alice.mu.se/pages/viewpage.action?pageId=838626085",
        "https://alice.mu.se/pages/viewpage.action?pageId=805320097",
        "https://alice.mu.se/pages/viewpage.action?pageId=838626085",
    ]
    pages = {
        "761947509": header("UMN-10743"),
        "838626085": header("UMN-13405"),
        "805320097": None,
    }
    storage = AsyncMock(side_effect=pages.get)
    assert await resolve_project_page_id(jira, "UMN-13405", storage) == "838626085"
    assert storage.await_count == 3

    pages["838626085"] = header("UMN-1340")
    with pytest.raises(SelectionError, match="none references the epic"):
        await resolve_project_page_id(jira, "UMN-13405", storage)

    pages["838626085"] = header("UMN-13405")
    pages["761947509"] = header("UMN-13405").replace(
        '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">UMN-13405'
        "</ac:parameter></ac:structured-macro>",
        '<a href="https://mu--se.atlassian.net/browse/UMN-13405">epic</a>',
    )
    with pytest.raises(SelectionError, match="multiple Confluence pages that reference"):
        await resolve_project_page_id(jira, "UMN-13405", storage)


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
                "Визуализация и копирайты — first",
                "5",
                "2026-08-01T10:01:00Z",
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
                "Визуализация и копирайты — second",
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

    # Once claimed, the task must be validated directly, not replaced by the next queued task.
    jira.children[1]["fields"]["status"]["id"] = "3"
    next_queued = await select_candidate(jira, now=NOW)
    assert next_queued is not None and next_queued.task_key == "UMN-104"
    assert await select_candidate(jira, now=NOW, claimed_task_key="UMN-102") == candidate
    # Claiming does not bypass the prerequisite task's review-age eligibility gate.
    jira.changelogs["UMN-101"] = [transition("2026-07-01T12:00:00Z", "5")]
    assert await select_candidate(jira, now=NOW, claimed_task_key="UMN-102") is None


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
                "Визуализация и копирайты — iteration 2",
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


async def test_old_pitch_task_no_longer_unlocks_work() -> None:
    jira = FakeJira(
        epics=[
            raw_issue(
                "UMN-200",
                "Epic",
                "3",
                "2026-08-01T00:00:00Z",
                issue_type="Epic",
                components=("Project - Hypothesis",),
            )
        ],
        children=[
            raw_issue(
                "UMN-201",
                "Проектирование и Питч",
                "5",
                "2026-08-20T10:00:00Z",
                parent="UMN-200",
            ),
            raw_issue(
                "UMN-202",
                "Аналитика",
                "1",
                "2026-08-20T10:00:30Z",
                parent="UMN-200",
            ),
        ],
        changelogs={"UMN-201": [transition("2026-08-25T12:00:00Z", "5")]},
    )

    assert await select_candidate(jira, now=NOW, task_title="Аналитика") is None
    assert jira.requested_changelogs == []


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
                "UMN-101", "Визуализация и копирайты", "5", "2026-07-01T00:00:00Z", parent="UMN-100"
            ),
            raw_issue(
                "UMN-102",
                "Расчет сверху и план тестирования",
                "1",
                "2026-07-01T01:00:01Z",
                parent="UMN-100",
            ),
            raw_issue(
                "UMN-103", "Визуализация и копирайты", "5", "2026-08-01T00:00:00Z", parent="UMN-100"
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


async def test_ambiguous_equidistant_prerequisite_pair_is_excluded() -> None:
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
                "UMN-101",
                "Визуализация и копирайты A",
                "5",
                "2026-08-01T10:00:00Z",
                parent="UMN-100",
            ),
            raw_issue(
                "UMN-102",
                "Визуализация и копирайты B",
                "5",
                "2026-08-01T10:00:00Z",
                parent="UMN-100",
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


def test_task_failure_names_selected_task_once() -> None:
    from sloperator.experiment_design_planner import FAILURE_PREFIX, task_failure

    assert task_failure(f"{FAILURE_PREFIX} selection changed", "UMN-1") == (
        f"{FAILURE_PREFIX} UMN-1: selection changed"
    )
    assert task_failure(f"{FAILURE_PREFIX} UMN-1: x", "UMN-1") == f"{FAILURE_PREFIX} UMN-1: x"
