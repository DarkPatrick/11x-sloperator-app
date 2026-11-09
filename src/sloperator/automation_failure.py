"""Shape Slack failure posts of the Jira-driven experiment automations.

Every failure of the design, analytics and finalizer jobs is addressed to the author of the
task's epic (the person who can fix the project setup) and carries a Jira link for every task key.
Before the post goes out, a task the run had already started is returned to its queue, so the
selector can pick it again once the problem is fixed.
"""

from __future__ import annotations

import logging
import re

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.config import Settings
from sloperator.experiment_design_selector import JiraRestReader, SelectionError
from sloperator.jira_task_release import release_task
from sloperator.slack_identity import DEFAULT_SLACK_MENTION_RESOLVER
from sloperator.team_channel import first_task_key

LOGGER = logging.getLogger(__name__)

# A bare Jira key (optionally in backticks) that is not already part of a URL or a link label.
BARE_JIRA_KEY_RE = re.compile(r"`(UMN-\d+)`|(?<![\w/\[|.-])(UMN-\d+)(?![\w\]-])")
ADDRESS_SUFFIX = ", please take a look. "


def link_jira_keys(text: str, jira_url: str) -> str:
    """Embed a Jira link into every bare task key so each task in a post is one click away."""
    base = jira_url.rstrip("/")

    def link(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        return f"[{key}]({base}/browse/{key})"

    return BARE_JIRA_KEY_RE.sub(link, text)


def _jira(settings: Settings) -> JiraRestReader | None:
    if not settings.jira_username or not settings.jira_api_token:
        return None
    return JiraRestReader(settings.jira_url, settings.jira_username, settings.jira_api_token)


async def epic_author_mention(
    client: AsyncWebClient, settings: Settings, epic_key: str
) -> str | None:
    """Mention the epic's author; fail open so the failure still posts."""
    jira = _jira(settings)
    if jira is None:
        return None
    try:
        name = await jira.issue_author(epic_key)
        if not name:
            return None
        user_id = await DEFAULT_SLACK_MENTION_RESOLVER.user_id_for_name(client, name)
    except Exception as error:
        LOGGER.warning(
            "Could not resolve the author of epic %s: %s", epic_key, type(error).__name__
        )
        return None
    # An unresolved name stays plain text; the delivery boundary may still turn it into a mention.
    return f"<@{user_id}>" if user_id else name


async def epic_key_for(settings: Settings, task_key: str) -> str | None:
    """Return the task's parent epic, or the key itself when it has no parent (an epic)."""
    jira = _jira(settings)
    if jira is None:
        return None
    try:
        return await jira.issue_parent_key(task_key)
    except SelectionError:
        return task_key
    except Exception as error:
        LOGGER.warning("Could not read the epic of %s: %s", task_key, type(error).__name__)
        return None


async def address_failure(
    client: AsyncWebClient,
    settings: Settings,
    message: str,
    *,
    task_key: str | None = None,
    epic_key: str | None = None,
) -> str:
    """Release the started task, address the failure to the epic author, link every Jira key.

    The failure prefix stays on the first line, so the daily digest still recognises the post.
    """
    message = message.strip()
    key = task_key or first_task_key(message)
    restored = await release_task(settings, key)
    if restored:
        message += f"\n{key} was returned to {restored}; the job will take it again once fixed."
    epic = epic_key or (await epic_key_for(settings, key) if key else None)
    author = await epic_author_mention(client, settings, epic) if epic else None
    linked = link_jira_keys(message, settings.jira_url)
    return f"{author}{ADDRESS_SUFFIX}{linked}" if author else linked
