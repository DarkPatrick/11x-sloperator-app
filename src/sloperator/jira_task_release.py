"""Return a task the automation started back to its queue after a failed run.

A start pass moves the selected task to In Progress (and may assign it and set its Start date).
When the run then fails, the task must go back to the status it came from, otherwise the
deterministic selector, which only picks Backlog / To Do tasks, never takes it again.

Only changes the service account itself made when it started the task are reverted, and only
while the task is still In Progress; a task a human moved, or one that already reached In Review
or Done, is left alone.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from sloperator.config import Settings
from sloperator.experiment_design_selector import JiraRestReader

LOGGER = logging.getLogger(__name__)

SERVICE_ACCOUNT_ID = "712020:e603f3a9-4b70-4ed8-866f-280460a661c5"
IN_PROGRESS = "in progress"
START_DATE_FIELD = "customfield_10312"
# A start older than this is not from the run that is failing now.
MAX_START_AGE = dt.timedelta(hours=26)
TRANSITION_LINE_RE = re.compile(r"^\s*(\d+):\s*(.*?)\s*->\s*(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class BotStart:
    previous_status: str
    started_at: dt.datetime
    start_date_set: bool
    previous_assignee: str | None
    assignee_changed: bool


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)


def _is_status(item: dict[str, Any]) -> bool:
    return item.get("fieldId") == "status" or str(item.get("field", "")).casefold() == "status"


def find_bot_start(histories: list[dict[str, Any]], now: dt.datetime) -> BotStart | None:
    """Locate the service account's latest move into In Progress and what it changed then."""
    status_changes = [
        (history, item)
        for history in histories
        for item in history.get("items", []) or []
        if isinstance(item, dict) and _is_status(item) and history.get("created")
    ]
    if not status_changes:
        return None
    history, item = max(status_changes, key=lambda pair: _parse(str(pair[0]["created"])))
    author = (history.get("author") or {}).get("accountId")
    if author != SERVICE_ACCOUNT_ID or str(item.get("toString", "")).casefold() != IN_PROGRESS:
        return None
    started_at = _parse(str(history["created"]))
    if now - started_at > MAX_START_AGE:
        return None
    start_date_set = False
    previous_assignee: str | None = None
    assignee_changed = False
    # The start pass assigns and dates the task just before the transition.
    window_start = started_at - dt.timedelta(minutes=30)
    for other in histories:
        created = other.get("created")
        if not created or (other.get("author") or {}).get("accountId") != SERVICE_ACCOUNT_ID:
            continue
        if not window_start <= _parse(str(created)) <= started_at:
            continue
        for change in other.get("items", []) or []:
            field = str(change.get("fieldId") or change.get("field") or "").casefold()
            if field in {START_DATE_FIELD, "start date"} and not change.get("from"):
                start_date_set = True
            elif field == "assignee" and change.get("to") == SERVICE_ACCOUNT_ID:
                assignee_changed = True
                previous_assignee = change.get("from") or None
    return BotStart(
        previous_status=str(item.get("fromString") or ""),
        started_at=started_at,
        start_date_set=start_date_set,
        previous_assignee=previous_assignee,
        assignee_changed=assignee_changed,
    )


async def _helper(settings: Settings, *args: str) -> str:
    workspace = settings.agent_workspace
    result = await asyncio.to_thread(
        subprocess.run,
        [
            str(workspace / ".venv" / "bin" / "python"),
            str(workspace / ".claude" / "jira" / "jira_issue.py"),
            *args,
            "--as-bot",
        ],
        cwd=workspace, capture_output=True, text=True, timeout=60, check=True,
    )
    return result.stdout


async def _transitions(settings: Settings, task_key: str) -> dict[str, str]:
    """Map target status (casefolded) to transition id."""
    output = await _helper(settings, "transitions", task_key)
    targets: dict[str, str] = {}
    for line in output.splitlines():
        match = TRANSITION_LINE_RE.match(line)
        if match:
            targets.setdefault(match.group(3).casefold(), match.group(1))
    return targets


async def _move_to(settings: Settings, task_key: str, target: str) -> None:
    """Transition to the target status, going through Backlog when there is no direct path."""
    wanted = target.casefold()
    for _ in range(2):
        targets = await _transitions(settings, task_key)
        if wanted in targets:
            await _helper(settings, "transition", task_key, "--id", targets[wanted])
            return
        if "backlog" not in targets:
            break
        await _helper(settings, "transition", task_key, "--id", targets["backlog"])
        if wanted == "backlog":
            return
    raise RuntimeError(f"Jira offers no path from In Progress back to {target}")


async def release_task(
    settings: Settings, task_key: str | None, *, now: dt.datetime | None = None
) -> str | None:
    """Undo the service account's start of a task after a failure; return the restored status.

    Never raises: a release problem is logged and the failure post goes out regardless.
    """
    if not task_key or not settings.jira_username or not settings.jira_api_token:
        return None
    moment = now or dt.datetime.now(dt.UTC)
    try:
        jira = JiraRestReader(settings.jira_url, settings.jira_username, settings.jira_api_token)
        start = find_bot_start(await jira.issue_changelog(task_key), moment)
        if start is None or not start.previous_status:
            return None
        await _move_to(settings, task_key, start.previous_status)
        if start.start_date_set:
            await _helper(settings, "set-start-date", task_key, "--clear")
        if start.assignee_changed and start.previous_assignee:
            await _helper(
                settings, "set-assignee", task_key, "--account-id", start.previous_assignee
            )
    except Exception as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else error
        LOGGER.error("Could not return %s to its queue after a failure: %s", task_key, detail)
        return None
    LOGGER.info("Returned %s to %s after a failed automation run", task_key, start.previous_status)
    return start.previous_status
