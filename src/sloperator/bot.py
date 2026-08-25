"""Slack event handlers."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Any

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import AgentOrchestrator, SubmitResult, thread_key
from sloperator.anomaly_alerts import AnomalyAlertResponder, is_anomaly_trigger
from sloperator.archive import ArchiveMiddleware
from sloperator.automation_controls import AutomationControls
from sloperator.config import Settings
from sloperator.experiment_config_check import (
    ExperimentConfigResponder,
    is_experiment_config_trigger,
)
from sloperator.mobile_health import MobileHealthResponder, is_mobile_health_trigger
from sloperator.payment_layer import PaymentLayerResponder, is_payment_layer_trigger
from sloperator.store import EventStore
from sloperator.subscription_flow import SubscriptionFlowResponder, is_subscription_flow_event
from sloperator.vpn import VpnError, VpnManager, VpnState
from sloperator.web_health import WebHealthResponder, is_web_health_trigger

LOGGER = logging.getLogger(__name__)
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
SUPPORTED_COMMANDS = {"", "help", "помощь", "ping", "status"}
CANCEL_COMMANDS = {"stop", "cancel", "стоп", "отмена"}


def normalize_command(text: str) -> str:
    """Remove a leading bot mention and normalize a message into a command."""
    return MENTION_RE.sub("", text, count=1).strip().lower()


def vpn_otp_from_command(command: str) -> str | None:
    """Return an OTP reserved for the VPN flow, regardless of its current state."""
    match = re.fullmatch(r"(?:vpn otp\s+)?(\d{6,8})", command)
    return match.group(1) if match is not None else None


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
                "• `vpn` / `vpn status` — статус корпоративного VPN\n"
                "• `vpn ready` / `готов` — начать подключение и запросить OTP\n"
                "• `vpn stop` — остановить VPN\n"
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


def is_trusted_channel_thread(event: Mapping[str, Any], settings: Settings) -> bool:
    """Match allowed-user replies in agent-enabled monitoring-channel threads."""
    return (
        event.get("user") in settings.conversation_user_ids
        and (
            event.get("channel_type") == "im"
            or event.get("channel")
            in {
                settings.anomaly_alert_channel,
                settings.subscription_flow_alert_channel,
                settings.experiment_finalizer_channel,
                settings.mobile_health_alert_channel,
            }
        )
        and isinstance(event.get("thread_ts"), str)
        and isinstance(event.get("text"), str)
        and isinstance(event.get("ts"), str)
    )


def create_app(
    settings: Settings,
    store: EventStore,
    orchestrator: AgentOrchestrator,
    vpn: VpnManager,
    subscription_flow_responder: SubscriptionFlowResponder | None = None,
    payment_layer_responder: PaymentLayerResponder | None = None,
    automation_controls: AutomationControls | None = None,
) -> AsyncApp:
    """Create and configure the Slack Bolt application."""
    app = AsyncApp(token=settings.bot_token, process_before_response=True)
    app.use(ArchiveMiddleware(store, app.client))
    anomaly_responder = AnomalyAlertResponder(settings, store, orchestrator)
    mobile_health_responder = MobileHealthResponder(settings, orchestrator)
    web_health_responder = WebHealthResponder(settings, orchestrator)
    subscription_flow_responder = subscription_flow_responder or SubscriptionFlowResponder(
        settings,
        store,
        orchestrator,
    )
    payment_layer_responder = payment_layer_responder or PaymentLayerResponder(
        settings, store, orchestrator
    )
    experiment_config_responder = ExperimentConfigResponder(settings, orchestrator)
    vpn_threads: set[tuple[str, str]] = set()

    @app.event("message")
    async def handle_message(
        event: Mapping[str, Any],
        client: AsyncWebClient,
    ) -> None:
        def enabled(key: str) -> bool:
            return automation_controls is None or not automation_controls.disabled("triggers", key)

        if enabled("analytics-anomaly") and is_anomaly_trigger(dict(event), settings):
            await anomaly_responder.handle(dict(event), client)
            return
        if enabled("mobile-health") and is_mobile_health_trigger(dict(event), settings):
            await mobile_health_responder.handle(dict(event), client)
            return
        if enabled("web-health") and is_web_health_trigger(dict(event), settings):
            await web_health_responder.handle(dict(event), client)
            return
        if enabled("subscription-flow") and is_subscription_flow_event(dict(event), settings):
            await subscription_flow_responder.handle(dict(event), client)
            return
        if enabled("payment-layer") and is_payment_layer_trigger(dict(event), settings):
            await payment_layer_responder.handle(dict(event), client)
            return
        if enabled("experiment-config") and is_experiment_config_trigger(dict(event)):
            await experiment_config_responder.handle(dict(event), client)
            return
        if event.get("subtype") is not None or event.get("bot_id") is not None:
            return

        user = event.get("user")
        channel = event.get("channel")
        text = event.get("text")
        trusted_channel_thread = is_trusted_channel_thread(event, settings)
        if user != settings.slack_user_id and not trusted_channel_thread:
            LOGGER.warning("Ignoring message from unauthorized Slack user %s", user)
            return
        message_ts = event.get("ts")
        active_thread_ts = reply_thread_ts(event)
        if trusted_channel_thread:
            assert isinstance(channel, str)
            assert isinstance(text, str)
            assert isinstance(message_ts, str)
            assert active_thread_ts is not None
            has_agent = await asyncio.to_thread(
                store.has_agent_thread,
                channel,
                active_thread_ts,
            )
            if not has_agent:
                LOGGER.debug("Ignoring owner reply in a non-agent monitoring thread")
                return
            command = normalize_command(text)
            if command in CANCEL_COMMANDS:
                if await orchestrator.cancel(channel, active_thread_ts):
                    response = "Остановлено. Выполнение агента в этом треде отменено."
                else:
                    response = "Сейчас в этом треде нет выполняющегося запроса."
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=active_thread_ts,
                    text=response,
                )
                return
            result = await orchestrator.submit(
                client,
                channel_id=channel,
                message_ts=message_ts,
                thread_ts=active_thread_ts,
                text=text,
                show_status=False,
                timeout_seconds=(
                    settings.mobile_health_timeout_seconds
                    if channel == settings.mobile_health_alert_channel
                    else None
                ),
                optional_reply=True,
                automated=channel == settings.mobile_health_alert_channel,
            )
            if result is SubmitResult.EXPIRED:
                LOGGER.debug(
                    "Ignoring reply in expired monitoring thread %s/%s",
                    channel,
                    active_thread_ts,
                )
            return
        if not isinstance(channel, str) or not channel.startswith("D") or not isinstance(text, str):
            LOGGER.debug("Ignoring malformed Slack message event")
            return

        if not isinstance(message_ts, str):
            LOGGER.debug("Ignoring Slack message without a timestamp")
            return

        command = normalize_command(text)
        current_thread_ts = thread_key(message_ts, active_thread_ts)
        current_thread = (channel, current_thread_ts)
        if command in {"vpn ready", "vpn connect", "vpn start", "готов"}:
            vpn_threads.add(current_thread)
            try:
                state = await vpn.connect()
                response = _vpn_state_response(state)
            except VpnError as error:
                response = f"VPN не запущен: {error}"
            await client.chat_postMessage(
                channel=channel,
                thread_ts=current_thread_ts,
                text=response,
            )
        elif command in {"vpn", "vpn status"}:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=current_thread_ts,
                text=_vpn_state_response(await vpn.state()),
            )
        elif command == "vpn stop":
            await vpn.stop()
            await client.chat_postMessage(
                channel=channel,
                thread_ts=current_thread_ts,
                text="VPN остановлен.",
            )
        elif (otp := vpn_otp_from_command(command)) is not None:
            # Slack may retry the same event after submit_otp has already changed
            # WAITING_OTP to CONNECTED. OTP-shaped messages must never fall through
            # to the default agent launcher, even after that state transition.
            state = await vpn.state()
            if state is VpnState.WAITING_OTP:
                try:
                    state = await vpn.submit_otp(otp)
                    response = _vpn_state_response(state)
                except VpnError as error:
                    response = f"VPN: {error}"
            else:
                response = _vpn_state_response(state)
            await client.chat_postMessage(
                channel=channel,
                thread_ts=current_thread_ts,
                text=response,
            )
        elif current_thread in vpn_threads:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=current_thread_ts,
                text=(
                    "Это служебный VPN-тред; сообщения из него не передаются агенту. "
                    "Для Claude или Codex начните новый Chat."
                ),
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
                thread_ts=current_thread_ts,
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
            elif result is SubmitResult.EXPIRED:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_key(message_ts, reply_thread_ts(event)),
                    text=(
                        "Сессия агента закрыта после 24 часов без активности. "
                        "Начните новый Chat, чтобы создать новую сессию."
                    ),
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
