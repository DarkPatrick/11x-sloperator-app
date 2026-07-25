"""Slack channel discovery, history backfill, and live event ingestion."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from slack_bolt.middleware.async_middleware import AsyncMiddleware
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_bolt.response import BoltResponse
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from sloperator.store import EventStore

LOGGER = logging.getLogger(__name__)


def _response_data(response: AsyncSlackResponse) -> Mapping[str, Any]:
    data = response.data
    if not isinstance(data, dict):
        raise TypeError("Slack Web API returned a non-object response")
    return data


async def _list_conversations(
    client: AsyncWebClient, conversation_types: str
) -> list[Mapping[str, Any]]:
    channels: list[Mapping[str, Any]] = []
    cursor: str | None = None
    while True:
        response = await client.conversations_list(
            types=conversation_types,
            exclude_archived=False,
            limit=200,
            cursor=cursor,
        )
        response_data = _response_data(response)
        channels.extend(response_data.get("channels", []))
        response_metadata = response_data.get("response_metadata")
        next_cursor = (
            response_metadata.get("next_cursor") if isinstance(response_metadata, Mapping) else None
        )
        cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
        if cursor is None:
            return channels


async def synchronize_archive(
    client: AsyncWebClient, store: EventStore, backfill_limit: int
) -> None:
    """Discover visible conversations and backfill bounded accessible history."""
    auth = await client.auth_test()
    await asyncio.to_thread(
        store.upsert_workspace,
        str(auth["team_id"]),
        str(auth.get("team", "")),
        str(auth["user_id"]),
    )

    channel_groups = (
        ("channel", "public_channel,private_channel"),
        ("direct", "im,mpim"),
    )
    all_channels: list[Mapping[str, Any]] = []
    for kind, conversation_types in channel_groups:
        channels = await _list_conversations(client, conversation_types)
        await asyncio.to_thread(store.upsert_channels, channels, kind)
        all_channels.extend(channels)

    accessible = [
        channel
        for channel in all_channels
        if channel.get("is_member") or channel.get("is_im") or channel.get("is_mpim")
    ]
    LOGGER.info(
        "Slack map synchronized: %d visible conversations, %d accessible",
        len(all_channels),
        len(accessible),
    )

    if backfill_limit == 0:
        return

    for channel in accessible:
        channel_id = channel.get("id")
        if not isinstance(channel_id, str):
            continue
        try:
            response = await client.conversations_history(channel=channel_id, limit=backfill_limit)
        except SlackApiError as error:
            LOGGER.warning(
                "Skipping history backfill for %s: %s",
                channel_id,
                error.response.get("error", "Slack API error"),
            )
            continue
        history_data = _response_data(response)
        messages: list[Mapping[str, Any]] = history_data.get("messages", [])
        await asyncio.to_thread(store.upsert_history_messages, channel_id, messages)

        for message in messages:
            latest_reply = message.get("latest_reply")
            if (
                message.get("reply_count")
                and isinstance(message.get("ts"), str)
                and isinstance(latest_reply, str)
                and not await asyncio.to_thread(
                    store.contains_message,
                    channel_id,
                    latest_reply,
                )
            ):
                try:
                    replies = await client.conversations_replies(
                        channel=channel_id,
                        ts=message["ts"],
                        limit=backfill_limit,
                    )
                except SlackApiError as error:
                    LOGGER.warning(
                        "Skipping thread backfill in %s: %s",
                        channel_id,
                        error.response.get("error", "Slack API error"),
                    )
                    continue
                await asyncio.to_thread(
                    store.upsert_history_messages,
                    channel_id,
                    _response_data(replies).get("messages", []),
                )


async def periodically_synchronize_archive(
    client: AsyncWebClient,
    store: EventStore,
    backfill_limit: int,
    interval_seconds: int,
) -> None:
    """Continuously reconcile history so missed events are eventually stored."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await synchronize_archive(client, store, backfill_limit)
        except Exception:
            LOGGER.exception("Periodic Slack archive synchronization failed")


async def persist_event(
    store: EventStore, body: Mapping[str, Any], event: Mapping[str, Any]
) -> None:
    """Persist one delivered Slack event without logging its content."""
    await asyncio.to_thread(store.record_event, body, event)


class ArchiveMiddleware(AsyncMiddleware):
    """Persist every delivered Events API callback before listener dispatch."""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    async def async_process(
        self,
        *,
        req: AsyncBoltRequest,
        resp: BoltResponse,
        next: Callable[[], Awaitable[BoltResponse]],
    ) -> BoltResponse | None:
        body = req.body
        event = body.get("event")
        if body.get("type") == "event_callback" and isinstance(event, Mapping):
            await persist_event(self.store, body, event)
        return await next()
