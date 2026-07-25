"""Slack event handlers."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from sloperator.archive import ArchiveMiddleware
from sloperator.config import Settings
from sloperator.store import EventStore

LOGGER = logging.getLogger(__name__)
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


def normalize_command(text: str) -> str:
    """Remove a leading bot mention and normalize a message into a command."""
    return MENTION_RE.sub("", text, count=1).strip().lower()


def response_for(command: str) -> str:
    """Return a deterministic response for a supported command."""
    match command:
        case "" | "help" | "помощь":
            return (
                "*Sloperator is online.*\n"
                "• `ping` — connectivity check\n"
                "• `status` — bot status\n"
                "• `help` — this message"
            )
        case "ping":
            return "pong"
        case "status":
            return "Sloperator is online and connected via Socket Mode."
        case _:
            return "Unknown command. Send `help` to see the available commands."


def reply_thread_ts(event: Mapping[str, Any]) -> str | None:
    """Keep replies in the active Slack Chat thread when one is present."""
    thread_ts = event.get("thread_ts")
    return thread_ts if isinstance(thread_ts, str) else None


def create_app(settings: Settings, store: EventStore) -> AsyncApp:
    """Create and configure the Slack Bolt application."""
    app = AsyncApp(token=settings.bot_token, process_before_response=True)
    app.use(ArchiveMiddleware(store))

    @app.event("message")
    async def handle_message(
        event: Mapping[str, Any],
        client: AsyncWebClient,
    ) -> None:
        if event.get("subtype") is not None or event.get("bot_id") is not None:
            return

        user = event.get("user")
        channel = event.get("channel")
        text = event.get("text")
        if user != settings.slack_user_id:
            LOGGER.warning("Ignoring message from unauthorized Slack user %s", user)
            return
        if not isinstance(channel, str) or not channel.startswith("D") or not isinstance(text, str):
            LOGGER.debug("Ignoring malformed Slack message event")
            return

        response = response_for(normalize_command(text))
        thread_ts = reply_thread_ts(event)
        if thread_ts is not None:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=response)
        else:
            await client.chat_postMessage(channel=channel, text=response)

    return app
