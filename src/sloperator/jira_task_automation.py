"""Selection and quota gates for persistent Jira task agents."""

from __future__ import annotations

import datetime as dt
import json
import logging
import asyncio
from dataclasses import dataclass
from typing import Any

from aiohttp import BasicAuth, ClientSession, ClientTimeout

from sloperator.claude_usage import ClaudeUsage, read_usage
from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.config import Settings

LOGGER = logging.getLogger(__name__)

BOARD_ID = 175
SERVICE_ACCOUNT_ID = "712020:e603f3a9-4b70-4ed8-866f-280460a661c5"
QUEUED_STATUSES = frozenset({"Backlog", "To Do"})
RETURNED_MARKER = "returned to work"

WORKER_PROMPT = f"""[claude]\n{AUTOMATED_RESPONSE_STYLE}\n\nYou are the worker for Jira task {{task_key}}. Work only on that task. Move it to In Progress, set Start date via customfield_10312, and perform the requested work. For Confluence use the service account's personal space if it exists; otherwise use the server. For analysis use parent https://alice.mu.se/spaces/CRO/pages/103614364/4.+Research+Sandbox+um, documentation https://alice.mu.se/spaces/CRO/pages/768842224/5.+Documentation+um, releases https://alice.mu.se/spaces/CRO/pages/103614361/3.+Product+Releases+um with the Product release template, hypotheses https://alice.mu.se/spaces/CRO/pages/103614359/2.+Hypothesis+um with the Hypotheses template, and generation https://alice.mu.se/spaces/CRO/pages/206146291/1.+Generation+um. If the result is small, keep it in Jira; Redash/Metabase is acceptable for queries or dashboards. Keep the task updated with concise factual notes. When done, return control to the reviewer with a short handoff; do not post Slack yourself."""
REVIEWER_PROMPT = f"""[claude]\n{AUTOMATED_RESPONSE_STYLE}\n\nYou are the reviewer and communication owner for Jira task {{task_key}}. Read all new Jira comments and all comments on the created Confluence page, verify the worker's result, and make corrections with the worker when needed. If information is missing, ask the task author in Jira and pause. When complete, add a concise Jira comment, set Due date via duedate, and transition with ID 181 to In Review. Keep all communication short and human-readable. Continue owning replies until the task is Done plus 24 hours without activity."""


def worker_prompt(task_key: str) -> str:
    return WORKER_PROMPT.format(task_key=task_key)


def reviewer_prompt(task_key: str) -> str:
    return REVIEWER_PROMPT.format(task_key=task_key)


async def run_hourly(settings: Settings, agent: Any, enabled: Any = lambda: True) -> None:
    """Hourly quota-gated launcher; task state is re-read before every launch."""
    while True:
        await asyncio.sleep(3600)
        if not enabled() or not settings.jira_username or not settings.jira_api_token:
            continue
        try:
            from sloperator.claude_usage import read_usage
            usage = await read_usage(settings.claude_cli, model=settings.claude_model)
            if not weekly_quota_allows_launch(usage, now=dt.datetime.now(dt.UTC)):
                LOGGER.info("Jira task agents held: Claude quota gate is closed")
                continue
            candidates = await JiraTaskReader(settings.jira_url, settings.jira_username, settings.jira_api_token).queued_tasks()
            if not candidates:
                continue
            task = candidates[0]
            link = agent.store.jira_task_agent_link(task.key)
            worker = await agent.execute_once(
                worker_prompt(task.key), 7200, job_name="jira-task-worker",
                existing_session_id=(link or {}).get("worker_session_id"),
            )
            agent.store.upsert_jira_task_agent_link(
                task.key, worker_session_id=worker.session_id, phase="reviewer"
            )
            reviewer = await agent.execute_once(
                reviewer_prompt(task.key) + f"\n\nWorker handoff:\n{worker.text}",
                7200,
                job_name="jira-task-reviewer",
                existing_session_id=(link or {}).get("reviewer_session_id"),
            )
            agent.store.upsert_jira_task_agent_link(
                task.key, reviewer_session_id=reviewer.session_id, phase="reviewer"
            )
        except Exception:
            LOGGER.exception("Jira task automation hourly run failed")


async def poll_active_tasks(settings: Settings, agent: Any, enabled: Any = lambda: True) -> None:
    """Ten-minute Jira poll that resumes the durable reviewer after task activity."""
    while True:
        await asyncio.sleep(600)
        if not enabled() or not settings.jira_username or not settings.jira_api_token:
            continue
        reader = JiraTaskReader(settings.jira_url, settings.jira_username, settings.jira_api_token)
        try:
            usage = await read_usage(settings.claude_cli, model=settings.claude_model)
            if not weekly_quota_allows_launch(usage, now=dt.datetime.now(dt.UTC)):
                continue
            for link in agent.store.active_jira_task_agent_links():
                task = await reader.task_snapshot(str(link["task_key"]))
                if task.status == "Done":
                    agent.store.upsert_jira_task_agent_link(task.key, phase="done", terminal_at=dt.datetime.now(dt.UTC).isoformat())
                    continue
                previous = link.get("last_jira_updated_at")
                if previous and task.updated_at <= dt.datetime.fromisoformat(str(previous)):
                    continue
                reviewer_id = link.get("reviewer_session_id")
                result = await agent.execute_once(
                    reviewer_prompt(task.key) + "\nRead all new Jira comments and continue the task.",
                    7200,
                    job_name="jira-task-reviewer",
                    existing_session_id=reviewer_id,
                )
                agent.store.upsert_jira_task_agent_link(
                    task.key, reviewer_session_id=result.session_id, phase="reviewer",
                    last_jira_updated_at=task.updated_at.isoformat(),
                )
        except Exception:
            LOGGER.exception("Jira task automation polling failed")


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

    async def task_snapshot(self, task_key: str) -> JiraTaskCandidate:
        async with ClientSession(auth=self.auth, timeout=self.timeout) as session:
            async with session.get(
                f"{self.base_url}/rest/api/3/issue/{task_key}",
                params={"fields": "summary,status,updated"},
            ) as response:
                if response.status >= 400:
                    raise RuntimeError(f"Jira task read failed with HTTP {response.status}")
                issue: dict[str, Any] = json.loads(await response.text())
        fields = issue["fields"]
        return JiraTaskCandidate(
            key=task_key,
            summary=str(fields.get("summary", "")),
            status=str(fields["status"]["name"]),
            updated_at=dt.datetime.fromisoformat(str(fields["updated"]).replace("Z", "+00:00")),
        )
