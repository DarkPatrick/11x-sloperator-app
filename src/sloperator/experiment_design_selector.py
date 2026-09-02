"""Deterministic Jira selection for the experiment-design pipeline."""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from aiohttp import BasicAuth, ClientSession, ClientTimeout

BOARD_ID = 175
BOARD_TIMEZONE = "Asia/Nicosia"
EPIC_COMPONENT = "Project - Hypothesis"
PITCH_TITLE = "проектирование и питч"
CALCULATION_TITLE = "расчет сверху и план тестирования"
PAIR_WINDOW_SECONDS = 60


class SelectionError(RuntimeError):
    """Raised when Jira cannot provide an authoritative selection snapshot."""


@dataclass(frozen=True, slots=True)
class Issue:
    key: str
    summary: str
    issue_type: str
    status_id: str
    component_names: tuple[str, ...]
    created_at: dt.datetime
    parent_key: str | None


@dataclass(frozen=True, slots=True)
class DesignCandidate:
    task_key: str
    epic_key: str
    pitch_key: str
    task_created_at: str
    pitch_reviewed_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class JiraReader(Protocol):
    async def board_configuration(self, board_id: int) -> dict[str, Any]: ...

    async def board_issues(self, board_id: int, filter_id: str) -> list[dict[str, Any]]: ...

    async def child_issues(self, epic_keys: list[str]) -> list[dict[str, Any]]: ...

    async def issue_changelog(self, issue_key: str) -> list[dict[str, Any]]: ...


class JiraRestReader:
    """Small read-only Jira Cloud client with complete pagination."""

    def __init__(self, base_url: str, username: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = BasicAuth(username, api_token)
        self.timeout = ClientTimeout(total=30, connect=10)

    async def _get(self, path: str, params: dict[str, str | int] | None = None) -> dict[str, Any]:
        async with (
            ClientSession(auth=self.auth, timeout=self.timeout) as session,
            session.get(f"{self.base_url}{path}", params=params) as response,
        ):
            body = await response.text()
            if response.status >= 400:
                raise SelectionError(f"Jira GET {path} failed with HTTP {response.status}")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as error:
                raise SelectionError(f"Jira GET {path} returned invalid JSON") from error
            if not isinstance(payload, dict):
                raise SelectionError(f"Jira GET {path} returned an invalid object")
            return payload

    async def board_configuration(self, board_id: int) -> dict[str, Any]:
        return await self._get(f"/rest/agile/1.0/board/{board_id}/configuration")

    async def _search(self, jql: str) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params: dict[str, str | int] = {
                "jql": jql,
                "maxResults": 100,
                "fields": "summary,issuetype,status,components,created,parent",
            }
            if token:
                params["nextPageToken"] = token
            page = await self._get("/rest/api/3/search/jql", params)
            batch = page.get("issues", [])
            if not isinstance(batch, list):
                raise SelectionError("Jira search response has no issue list")
            issues.extend(item for item in batch if isinstance(item, dict))
            token_value = page.get("nextPageToken")
            token = str(token_value) if token_value else None
            if page.get("isLast") is True or not token or not batch:
                return issues

    async def board_issues(self, board_id: int, filter_id: str) -> list[dict[str, Any]]:
        del board_id
        return await self._search(
            f'filter = {filter_id} AND issuetype = Epic AND component = "{EPIC_COMPONENT}" '
            "ORDER BY created ASC, key ASC"
        )

    async def child_issues(self, epic_keys: list[str]) -> list[dict[str, Any]]:
        if not epic_keys:
            return []
        issues: list[dict[str, Any]] = []
        for offset in range(0, len(epic_keys), 40):
            quoted = ", ".join(f'"{key}"' for key in epic_keys[offset : offset + 40])
            jql = f"parent in ({quoted}) ORDER BY created ASC, key ASC"
            issues.extend(await self._search(jql))
        return issues

    async def issue_changelog(self, issue_key: str) -> list[dict[str, Any]]:
        histories: list[dict[str, Any]] = []
        start_at = 0
        while True:
            page = await self._get(
                f"/rest/api/3/issue/{issue_key}/changelog",
                {"startAt": start_at, "maxResults": 100},
            )
            batch = page.get("values", [])
            if not isinstance(batch, list):
                raise SelectionError("Jira changelog response has no history list")
            histories.extend(item for item in batch if isinstance(item, dict))
            start_at += len(batch)
            if not batch or start_at >= int(page.get("total", start_at)):
                return histories


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "\u0435")
    return " ".join(value.split())


def _parse_datetime(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SelectionError(f"Jira timestamp has no timezone: {value}")
    return parsed.astimezone(dt.UTC)


def _parse_issue(raw: dict[str, Any]) -> Issue:
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        raise SelectionError("Jira issue has no fields object")
    status = fields.get("status") or {}
    issue_type = fields.get("issuetype") or {}
    parent = fields.get("parent") or {}
    components = fields.get("components") or []
    try:
        return Issue(
            key=str(raw["key"]),
            summary=str(fields["summary"]),
            issue_type=str(issue_type["name"]),
            status_id=str(status["id"]),
            component_names=tuple(
                str(component["name"])
                for component in components
                if isinstance(component, dict) and component.get("name")
            ),
            created_at=_parse_datetime(str(fields["created"])),
            parent_key=str(parent["key"]) if parent.get("key") else None,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SelectionError(f"Jira issue {raw.get('key', '<unknown>')} is incomplete") from error


def _column_statuses(configuration: dict[str, Any]) -> dict[str, frozenset[str]]:
    columns = configuration.get("columnConfig", {}).get("columns", [])
    if not isinstance(columns, list):
        raise SelectionError("Jira board configuration has no columns")
    result: dict[str, frozenset[str]] = {}
    for column in columns:
        if not isinstance(column, dict) or not column.get("name"):
            continue
        statuses = column.get("statuses", [])
        result[_normalize(str(column["name"]))] = frozenset(
            str(status["id"])
            for status in statuses
            if isinstance(status, dict) and status.get("id") is not None
        )
    return result


def _statuses_for(columns: dict[str, frozenset[str]], names: set[str]) -> frozenset[str]:
    wanted = {_normalize(name) for name in names}
    return frozenset().union(*(statuses for name, statuses in columns.items() if name in wanted))


def _previous_calendar_month(moment: dt.datetime) -> dt.datetime:
    year, month = moment.year, moment.month - 1
    if month == 0:
        year, month = year - 1, 12
    while True:
        try:
            return moment.replace(year=year, month=month)
        except ValueError:
            moment = moment.replace(day=moment.day - 1)


def _latest_transition(
    histories: list[dict[str, Any]], eligible_statuses: frozenset[str]
) -> dt.datetime | None:
    matches: list[dt.datetime] = []
    for history in histories:
        created = history.get("created")
        items = history.get("items", [])
        if not created or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if (
                item.get("fieldId") != "status"
                and _normalize(str(item.get("field", ""))) != "status"
            ):
                continue
            if str(item.get("to")) in eligible_statuses:
                matches.append(_parse_datetime(str(created)))
    return max(matches, default=None)


def _pair_children(children: list[Issue]) -> list[tuple[Issue, Issue]]:
    pairs: list[tuple[Issue, Issue]] = []
    for parent_key in sorted({issue.parent_key for issue in children if issue.parent_key}):
        siblings = [issue for issue in children if issue.parent_key == parent_key]
        pitches = [issue for issue in siblings if PITCH_TITLE in _normalize(issue.summary)]
        calculations = [
            issue for issue in siblings if CALCULATION_TITLE in _normalize(issue.summary)
        ]
        used: set[str] = set()
        for calculation in sorted(calculations, key=lambda issue: (issue.created_at, issue.key)):
            options = [
                pitch
                for pitch in pitches
                if pitch.key not in used
                and 0
                <= (calculation.created_at - pitch.created_at).total_seconds()
                <= PAIR_WINDOW_SECONDS
            ]
            if not options:
                continue
            closest_seconds = min(
                (calculation.created_at - pitch.created_at).total_seconds() for pitch in options
            )
            closest = [
                pitch
                for pitch in options
                if (calculation.created_at - pitch.created_at).total_seconds() == closest_seconds
            ]
            if len(closest) != 1:
                continue
            used.add(closest[0].key)
            pairs.append((closest[0], calculation))
    return pairs


async def select_candidate(
    jira: JiraReader,
    *,
    now: dt.datetime | None = None,
    board_id: int = BOARD_ID,
) -> DesignCandidate | None:
    """Select the oldest eligible calculation task from one authoritative snapshot."""
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    configuration = await jira.board_configuration(board_id)
    filter_id = configuration.get("filter", {}).get("id")
    if filter_id is None:
        raise SelectionError("Jira board configuration has no filter id")
    columns = _column_statuses(configuration)
    epic_statuses = _statuses_for(columns, {"In Progress"})
    calculation_statuses = _statuses_for(columns, {"Backlog", "Бэклог", "To Do"})
    reviewed_statuses = _statuses_for(columns, {"In Review", "Done"})
    if not epic_statuses or not calculation_statuses or not reviewed_statuses:
        raise SelectionError("Required Jira board columns or status mappings are missing")

    board_issues = [_parse_issue(raw) for raw in await jira.board_issues(board_id, str(filter_id))]
    epics = [
        issue
        for issue in board_issues
        if _normalize(issue.issue_type) in {"epic", "эпик"}
        and issue.status_id in epic_statuses
        and EPIC_COMPONENT in issue.component_names
    ]
    children = [_parse_issue(raw) for raw in await jira.child_issues([epic.key for epic in epics])]
    epic_keys = {epic.key for epic in epics}
    eligible: list[DesignCandidate] = []
    lower_bound = _previous_calendar_month(moment.astimezone(ZoneInfo(BOARD_TIMEZONE))).astimezone(
        dt.UTC
    )
    for pitch, calculation in _pair_children(children):
        if calculation.parent_key not in epic_keys:
            continue
        if (
            calculation.status_id not in calculation_statuses
            or pitch.status_id not in reviewed_statuses
        ):
            continue
        transition = _latest_transition(await jira.issue_changelog(pitch.key), reviewed_statuses)
        if transition is None or not lower_bound <= transition <= moment:
            continue
        eligible.append(
            DesignCandidate(
                task_key=calculation.key,
                epic_key=calculation.parent_key,
                pitch_key=pitch.key,
                task_created_at=calculation.created_at.isoformat(),
                pitch_reviewed_at=transition.isoformat(),
            )
        )

    def candidate_order(candidate: DesignCandidate) -> tuple[dt.datetime, int]:
        numeric_key = re.search(r"\d+$", candidate.task_key)
        return (
            _parse_datetime(candidate.task_created_at),
            int(numeric_key.group()) if numeric_key else 0,
        )

    return min(
        eligible,
        key=candidate_order,
        default=None,
    )
