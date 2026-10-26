"""Route Jira-driven experiment notifications to the channel of the epic's team."""

from __future__ import annotations

import logging
import re

from aiohttp import ClientError

from sloperator.config import Settings
from sloperator.experiment_design_selector import JiraRestReader, SelectionError

LOGGER = logging.getLogger(__name__)

RETENTION_DEBT_LABEL = "ug-ux-retention-debt"
TASK_KEY_RE = re.compile(r"\b(UMN-\d+)\b")


def first_task_key(text: str) -> str | None:
    """Return the first Jira key named in a notification or failure line."""
    match = TASK_KEY_RE.search(text)
    return match.group(1) if match else None


async def channel_for_task(settings: Settings, task_key: str | None, default: str) -> str:
    """Return the retention-debt channel when the task's epic carries its label.

    The key may name the epic itself or a task under it. Any Jira problem keeps the job's
    default channel: a misrouted post is recoverable, a lost one is not.
    """
    if not task_key or not settings.retention_debt_channel:
        return default
    if not settings.jira_username or not settings.jira_api_token:
        return default
    jira = JiraRestReader(settings.jira_url, settings.jira_username, settings.jira_api_token)
    try:
        epic_labels = await jira.epic_labels(task_key)
    except (SelectionError, ClientError, TimeoutError) as error:
        LOGGER.warning("Could not read epic labels for %s; using %s: %s", task_key, default, error)
        return default
    if RETENTION_DEBT_LABEL in epic_labels:
        return settings.retention_debt_channel
    return default
