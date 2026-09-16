"""Resolve exact Slack profile names to native mentions at the delivery boundary."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

LOGGER = logging.getLogger(__name__)

_PROTECTED_SLACK_TEXT = re.compile(
    r"(```[\s\S]*?```|`[^`\n]*`|<[^>\n]+>|\[[^\]\n]+\]\([^\)\n]+\))"
)
_RESOLUTION_GAP = re.compile(
    r"(?P<mention><@U[A-Z0-9]+>)\s*"
    r"\((?:no\s+)?Slack\s+(?:user\s+)?id\s+could\s+(?:not\s+)?be\s+resolved"
    r"(?:\s+for\s+(?:him|her|them))?\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Directory:
    aliases: tuple[tuple[str, str], ...]
    expires_at: float


class SlackMentionResolver:
    """Cache the visible workspace directory and replace unambiguous exact names."""

    def __init__(self, ttl_seconds: float = 3600) -> None:
        self.ttl_seconds = ttl_seconds
        self._directories: dict[str, _Directory] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def resolve(self, client: AsyncWebClient, text: str) -> str:
        if not text or " " not in text:
            return text
        aliases = await self._aliases(client)
        if not aliases:
            return text
        parts = _PROTECTED_SLACK_TEXT.split(text)
        for index in range(0, len(parts), 2):
            parts[index] = self._replace_plain_text(parts[index], aliases)
        return _RESOLUTION_GAP.sub(r"\g<mention>", "".join(parts))

    async def _aliases(self, client: AsyncWebClient) -> tuple[tuple[str, str], ...]:
        token = str(getattr(client, "token", ""))
        cache_key = hashlib.sha256(token.encode()).hexdigest()
        cached = self._directories.get(cache_key)
        now = time.monotonic()
        if cached is not None and cached.expires_at > now:
            return cached.aliases

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._directories.get(cache_key)
            now = time.monotonic()
            if cached is not None and cached.expires_at > now:
                return cached.aliases
            aliases = await self._fetch_aliases(client)
            self._directories[cache_key] = _Directory(aliases, now + self.ttl_seconds)
            return aliases

    @staticmethod
    async def _fetch_aliases(client: AsyncWebClient) -> tuple[tuple[str, str], ...]:
        candidates: dict[str, set[str]] = {}
        cursor = ""
        while True:
            response = await client.users_list(limit=200, **({"cursor": cursor} if cursor else {}))
            members = response.get("members", [])
            if isinstance(members, list):
                for member in members:
                    if (
                        not isinstance(member, dict)
                        or member.get("deleted")
                        or member.get("is_bot")
                    ):
                        continue
                    user_id = member.get("id")
                    if not isinstance(user_id, str) or not user_id.startswith("U"):
                        continue
                    profile = member.get("profile", {})
                    if not isinstance(profile, dict):
                        profile = {}
                    for value in (
                        profile.get("display_name"),
                        profile.get("real_name"),
                        member.get("real_name"),
                    ):
                        if not isinstance(value, str):
                            continue
                        alias = " ".join(value.split())
                        # Single words are too likely to be ordinary prose. Corporate real names
                        # have at least two words; display-name-only users remain plain text.
                        if len(alias) >= 5 and " " in alias:
                            candidates.setdefault(alias, set()).add(user_id)
            metadata = response.get("response_metadata", {})
            next_cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
            cursor = next_cursor.strip() if isinstance(next_cursor, str) else ""
            if not cursor:
                break
        unique = ((alias, next(iter(ids))) for alias, ids in candidates.items() if len(ids) == 1)
        return tuple(sorted(unique, key=lambda item: len(item[0]), reverse=True))

    @staticmethod
    def _replace_plain_text(text: str, aliases: tuple[tuple[str, str], ...]) -> str:
        for alias, user_id in aliases:
            text = re.sub(
                rf"(?<![\w@]){re.escape(alias)}(?![\w])",
                f"<@{user_id}>",
                text,
            )
        return text


DEFAULT_SLACK_MENTION_RESOLVER = SlackMentionResolver()


async def resolve_payload_mentions(
    client: AsyncWebClient,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Resolve text-bearing message fields, failing open when the directory is unavailable."""
    resolved = dict(payload)
    try:
        for field in ("text", "markdown_text"):
            value = resolved.get(field)
            if isinstance(value, str):
                resolved[field] = await DEFAULT_SLACK_MENTION_RESOLVER.resolve(client, value)
        for field in ("blocks", "attachments"):
            if field in resolved:
                resolved[field] = await _resolve_rich_text(client, resolved[field])
    except Exception as error:
        LOGGER.warning("Could not resolve outgoing Slack profile names: %s", type(error).__name__)
        return payload
    return resolved


async def _resolve_rich_text(client: AsyncWebClient, value: Any) -> Any:
    if isinstance(value, list):
        return [await _resolve_rich_text(client, item) for item in value]
    if isinstance(value, dict):
        result = dict(value)
        for key, item in value.items():
            if key == "text" and isinstance(item, str):
                result[key] = await DEFAULT_SLACK_MENTION_RESOLVER.resolve(client, item)
            elif isinstance(item, (dict, list)):
                result[key] = await _resolve_rich_text(client, item)
        return result
    return value
