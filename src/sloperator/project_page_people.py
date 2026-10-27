"""Guarantee that a results post mentions everyone from the project page header.

The finalizer agent is told to mention every person in the project page's DRI / Project owner
and Team rows, but that is only a prompt instruction. This module re-reads the header at the
delivery boundary and appends whoever the agent left out. It fails open: if Confluence or the
Slack directory is unavailable, the post goes out unchanged.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout
from slack_sdk.web.async_client import AsyncWebClient

from sloperator.slack_identity import DEFAULT_SLACK_MENTION_RESOLVER

LOGGER = logging.getLogger(__name__)

DEFAULT_CONFLUENCE_URL = "https://alice.mu.se"
USER_DIRECTORY_RELATIVE = Path("context") / "confluence_userkey_to_display_name.json"
HEADER_ROLES = ("dri / project owner", "team")

_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HEADER_CELL_RE = re.compile(r"<th\b[^>]*>(.*?)</th>", re.IGNORECASE | re.DOTALL)
_USERKEY_RE = re.compile(r"<ri:user\b[^>]*\bri:userkey=\"([^\"]+)\"", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SLACK_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


@dataclass(frozen=True)
class HeaderPerson:
    userkey: str
    display_name: str | None
    slack_id: str | None


def header_userkeys(storage: str) -> tuple[str, ...]:
    """Return userkeys from the DRI / Project owner and Team rows, in page order, deduplicated."""
    keys: list[str] = []
    for row in _ROW_RE.findall(storage):
        header = _HEADER_CELL_RE.search(row)
        if header is None:
            continue
        label = " ".join(html.unescape(_TAG_RE.sub(" ", header.group(1))).split()).lower()
        if label not in HEADER_ROLES:
            continue
        for key in _USERKEY_RE.findall(row[header.end():]):
            if key not in keys:
                keys.append(key)
    return tuple(keys)


def missing_mentions(text: str, people: Iterable[HeaderPerson]) -> list[str]:
    """Return the mention tokens for header people the notification does not name yet."""
    present_ids = set(_SLACK_MENTION_RE.findall(text))
    plain_text = _SLACK_MENTION_RE.sub(" ", text)
    missing: list[str] = []
    for person in people:
        if person.slack_id is not None:
            if person.slack_id in present_ids:
                continue
            if person.display_name and person.display_name in plain_text:
                continue
            token = f"<@{person.slack_id}>"
        elif person.display_name:
            if person.display_name in plain_text:
                continue
            # The delivery boundary turns an exact profile name into a native mention.
            token = person.display_name
        else:
            continue
        if token not in missing:
            missing.append(token)
    return missing


def add_mentions(text: str, mentions: list[str]) -> str:
    """Append mentions to the line after the heading, or insert that line when there is none."""
    if not mentions:
        return text
    lines = text.splitlines()
    addition = " ".join(mentions)
    if len(lines) > 1 and _SLACK_MENTION_RE.search(lines[1]) and not lines[1].lstrip().startswith(
        ("•", "-", "*")
    ):
        lines[1] = lines[1].rstrip() + " " + addition
    else:
        lines.insert(1, addition)
    return "\n".join(lines)


class ConfluenceHeaderReader:
    """Minimal read-only Confluence DC client for the project page header."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.timeout = ClientTimeout(total=30, connect=10)

    @classmethod
    def from_environment(cls) -> ConfluenceHeaderReader | None:
        token = (
            os.environ.get("CONFLUENCE_PERSONAL_TOKEN") or os.environ.get("CONFLUENCE_TOKEN") or ""
        ).strip()
        if not token:
            return None
        base_url = os.environ.get("CONFLUENCE_BASE_URL", DEFAULT_CONFLUENCE_URL).strip()
        return cls(base_url or DEFAULT_CONFLUENCE_URL, token)

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        async with (
            ClientSession(headers=self.headers, timeout=self.timeout) as session,
            session.get(f"{self.base_url}{path}", params=params) as response,
        ):
            if response.status >= 400:
                raise RuntimeError(f"Confluence GET {path} failed with HTTP {response.status}")
            payload = json.loads(await response.text())
            if not isinstance(payload, dict):
                raise RuntimeError(f"Confluence GET {path} returned an invalid object")
            return payload

    async def page_storage(self, page_id: str) -> str:
        page = await self._get(f"/rest/api/content/{page_id}", {"expand": "body.storage"})
        value = page.get("body", {}).get("storage", {}).get("value", "")
        return value if isinstance(value, str) else ""

    async def display_name(self, userkey: str) -> str | None:
        user = await self._get("/rest/api/user", {"key": userkey})
        name = user.get("displayName")
        return name.strip() if isinstance(name, str) and name.strip() else None


def local_display_names(workspace: Path) -> dict[str, str]:
    """Read the private userkey -> display name directory of the agent workspace, if present."""
    path = workspace / USER_DIRECTORY_RELATIVE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        LOGGER.warning("Could not read the Confluence user directory: %s", type(error).__name__)
        return {}
    if isinstance(value, dict) and isinstance(value.get("users"), dict):
        value = value["users"]
    if not isinstance(value, dict):
        return {}
    return {
        key: name.strip()
        for key, name in value.items()
        if isinstance(key, str) and isinstance(name, str) and name.strip()
    }


async def header_people(
    client: AsyncWebClient,
    reader: ConfluenceHeaderReader,
    page_id: str,
    workspace: Path,
    slack_lookup: Callable[[AsyncWebClient, str], Awaitable[str | None]] | None = None,
) -> list[HeaderPerson]:
    lookup = slack_lookup or DEFAULT_SLACK_MENTION_RESOLVER.user_id_for_name
    keys = header_userkeys(await reader.page_storage(page_id))
    directory = local_display_names(workspace) if keys else {}
    people: list[HeaderPerson] = []
    for key in keys:
        name: str | None
        try:
            name = await reader.display_name(key)
        except Exception as error:
            LOGGER.warning("Could not resolve Confluence user %s: %s", key, type(error).__name__)
            name = None
        name = name or directory.get(key)
        slack_id = await lookup(client, name) if name else None
        people.append(HeaderPerson(key, name, slack_id))
    return people


async def ensure_header_mentions(
    client: AsyncWebClient,
    notification: str,
    page_id: str | None,
    workspace: Path,
    reader: ConfluenceHeaderReader | None = None,
) -> str:
    """Add every DRI / Team member the notification misses; never block the post."""
    if not page_id:
        return notification
    reader = reader or ConfluenceHeaderReader.from_environment()
    if reader is None:
        LOGGER.warning("No Confluence token; header mentions of page %s not verified", page_id)
        return notification
    try:
        people = await header_people(client, reader, page_id, workspace)
    except Exception as error:
        LOGGER.warning(
            "Could not verify header mentions of page %s: %s", page_id, type(error).__name__
        )
        return notification
    unresolved = [person.userkey for person in people if person.display_name is None]
    if unresolved:
        LOGGER.warning("Header users of page %s have no display name: %s", page_id, unresolved)
    missing = missing_mentions(notification, people)
    if missing:
        LOGGER.warning("Adding header mentions the agent left out on page %s: %s", page_id, missing)
    return add_mentions(notification, missing)
