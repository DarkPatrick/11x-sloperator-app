"""Selection and quota gates for persistent Jira task agents."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from aiohttp import BasicAuth, ClientSession, ClientTimeout

from sloperator.claude_usage import ClaudeUsage
from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE

BOARD_ID = 175
SERVICE_ACCOUNT_ID = "712020:e603f3a9-4b70-4ed8-866f-280460a661c5"
QUEUED_STATUSES = frozenset({"Backlog", "To Do"})
RETURNED_MARKER = "returned to work"

WORKER_PROMPT = f"""[claude]\n{AUTOMATED_RESPONSE_STYLE}\n\nYou are the worker for Jira task {{task_key}}. Work only on that task. Move it to In Progress, set Start date via customfield_10312, and perform the requested work. Use the appropriate Confluence parent and required template when a page is needed. Keep the task updated with concise factual notes. When done, return control to the reviewer with a short handoff; do not post Slack yourself."""
REVIEWER_PROMPT = f"""[claude]\n{AUTOMATED_RESPONSE_STYLE}\n\nYou are the reviewer and communication owner for Jira task {{task_key}}. Read all new Jira and Confluence comments, verify the worker's result, and make corrections with the worker when needed. If information is missing, ask the task author in Jira and pause. When complete, add a concise Jira comment, set Due date via duedate, and transition with ID 181 to In Review. Keep all communication short and human-readable."""


def worker_prompt(task_key: str) -> str:
    return WORKER_PROMPT.format(task_key=task_key)


def reviewer_prompt(task_key: str) -> str:
    return REVIEWER_PROMPT.format(task_key=task_key)


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
