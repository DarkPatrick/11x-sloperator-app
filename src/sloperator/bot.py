"""Slack event handlers."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import AgentOrchestrator, SubmitResult, thread_key
from sloperator.archive import ArchiveMiddleware
from sloperator.config import Settings
from sloperator.store import EventStore
from sloperator.vpn import VpnError, VpnManager, VpnState

LOGGER = logging.getLogger(__name__)
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
SUPPORTED_COMMANDS = {"", "help", "помощь", "ping", "status"}
CANCEL_COMMANDS = {"stop", "cancel", "стоп", "отмена"}


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
                "• `stop` / `отмена` — остановить агента в этом треде\n"
                "• `next: запрос` — поставить отдельный следующий ход\n"
                "• `vpn` / `vpn status` / `vpn stop` — корпоративный VPN\n"
                "• `help` — this message\n\n"
                "Сообщение во время работы уточняет текущий ход агента.\n"
                "Любой другой текст запускает или продолжает сессию в этом треде.\n"
                "По умолчанию: Claude Opus. Выбор для нового Chat:\n"
                "• `[claude] запрос`\n"
                "• `[claude:opus] запрос`\n"
                "• `[codex:gpt-5.6-sol] запрос`"
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


def create_app(
    settings: Settings,
    store: EventStore,
    orchestrator: AgentOrchestrator,
    vpn: VpnManager,
) -> AsyncApp:
    """Create and configure the Slack Bolt application."""
    app = AsyncApp(token=settings.bot_token, process_before_response=True)
    app.use(ArchiveMiddleware(store, app.client))

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

        message_ts = event.get("ts")
        if not isinstance(message_ts, str):
            LOGGER.debug("Ignoring Slack message without a timestamp")
            return

        command = normalize_command(text)
        active_thread_ts = reply_thread_ts(event)
        if command in {"vpn", "vpn connect", "vpn start"}:
            try:
                state = await vpn.connect()
                response = _vpn_state_response(state)
            except VpnError as error:
                response = f"VPN не запущен: {error}"
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_key(message_ts, active_thread_ts),
                text=response,
            )
        elif command == "vpn status":
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_key(message_ts, active_thread_ts),
                text=_vpn_state_response(await vpn.state()),
            )
        elif command == "vpn stop":
            await vpn.stop()
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_key(message_ts, active_thread_ts),
                text="VPN остановлен.",
            )
        elif (
            (otp_match := re.fullmatch(r"(?:vpn otp\s+)?(\d{6,8})", command))
            and await vpn.state() is VpnState.WAITING_OTP
        ):
            try:
                state = await vpn.submit_otp(otp_match.group(1))
                response = _vpn_state_response(state)
            except VpnError as error:
                response = f"VPN: {error}"
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_key(message_ts, active_thread_ts),
                text=response,
            )
        elif command in CANCEL_COMMANDS:
            if active_thread_ts is None:
                response = "Команда остановки работает внутри треда активной сессии."
            elif await orchestrator.cancel(channel, active_thread_ts):
                response = "Остановлено. Выполнение агента в этом треде отменено."
            else:
                response = "Сейчас в этом треде нет выполняющегося запроса."
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_key(message_ts, active_thread_ts),
                text=response,
            )
        elif command in SUPPORTED_COMMANDS:
            response = response_for(command)
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_key(message_ts, reply_thread_ts(event)),
                text=response,
            )
        else:
            result = await orchestrator.submit(
                client,
                channel_id=channel,
                message_ts=message_ts,
                thread_ts=thread_key(message_ts, reply_thread_ts(event)),
                text=text,
            )
            if result is SubmitResult.STEERED:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_key(message_ts, reply_thread_ts(event)),
                    text="Уточнение передано активному агенту.",
                )

    return app


def _vpn_state_response(state: VpnState) -> str:
    match state:
        case VpnState.CONNECTED:
            return "VPN подключён; агентский HTTP/HTTPS-трафик идёт через VPN proxy."
        case VpnState.WAITING_OTP:
            return (
                "LDAP принят. VPN ожидает одноразовый код - пришлите сюда "
                "6-8 цифр отдельным сообщением."
            )
        case VpnState.CONNECTING:
            return "VPN подключается…"
        case VpnState.STOPPED:
            return "VPN остановлен."
        case VpnState.FAILED:
            return "VPN connection failed."
