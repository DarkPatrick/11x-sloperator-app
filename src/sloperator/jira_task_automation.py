"""Selection and quota gates for persistent Jira task agents."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from aiohttp import BasicAuth, ClientSession, ClientTimeout

from sloperator.claude_usage import ClaudeUsage

BOARD_ID = 175
SERVICE_ACCOUNT_ID = "712020:e603f3a9-4b70-4ed8-866f-280460a661c5"
QUEUED_STATUSES = frozenset({"Backlog", "To Do"})
RETURNED_MARKER = "returned to work"


@dataclass(frozen=True, slots=True)
class JiraTaskCandidate:
    key: str
    summary: str
    status: str
    updated_at: dt.datetime
    project_url: str | None = None


def weekly_quota_allows_launch(usage: ClaudeUsage, *, now: dt.datetime) -> bool:
    """Require >50% five-hour headroom and enough weekly headroom for remaining days."""
    if usage.session_remaining_percent <= 50:
        return False
    if usage.week_remaining_percent > 90:
        return True
    reset = _parse_reset_day(usage.week_reset_text, now)
    remaining_days = max(1, (reset.date() - now.date()).days)
    return usage.week_remaining_percent > 14 * remaining_days


def _parse_reset_day(value: str, now: dt.datetime) -> dt.datetime:
    # /usage gives human text; only the calendar day is needed for the conservative gate.
    cleaned = value.replace("(UTC)", "").strip()
    for fmt in ("%b %d, %I:%M%p", "%b %d, %I%p"):
        try:
            parsed = dt.datetime.strptime(f"{cleaned} {now.year}", f"{fmt} %Y").replace(tzinfo=dt.UTC)
            if parsed < now - dt.timedelta(days=180):
                parsed = parsed.replace(year=now.year + 1)
            return parsed
        except ValueError:
            continue
    raise ValueError(f"unparseable Claude reset time: {value}")


class JiraTaskReader:
    def __init__(self, base_url: str, username: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = BasicAuth(username, api_token)
        self.timeout = ClientTimeout(total=30, connect=10)

    async def queued_tasks(self) -> list[JiraTaskCandidate]:
        jql = (
            f'project = UMN AND assignee = "{SERVICE_ACCOUNT_ID}" '
            'AND status in (Backlog, "To Do") ORDER BY updated ASC, key ASC'
        )
        async with ClientSession(auth=self.auth, timeout=self.timeout) as session:
            async with session.get(
                f"{self.base_url}/rest/api/3/search/jql",
                params={"jql": jql, "maxResults": 100, "fields": "summary,status,updated"},
            ) as response:
                if response.status >= 400:
                    raise RuntimeError(f"Jira task search failed with HTTP {response.status}")
                payload: dict[str, Any] = json.loads(await response.text())
        result: list[JiraTaskCandidate] = []
        for issue in payload.get("issues", []):
            fields = issue.get("fields", {})
            status = fields.get("status", {}).get("name")
            if status not in QUEUED_STATUSES:
                continue
            result.append(
                JiraTaskCandidate(
                    key=str(issue["key"]),
                    summary=str(fields.get("summary", "")),
                    status=status,
                    updated_at=dt.datetime.fromisoformat(str(fields["updated"]).replace("Z", "+00:00")),
                )
            )
        return result
