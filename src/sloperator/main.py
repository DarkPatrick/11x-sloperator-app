"""Application lifecycle."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Mapping
from contextlib import suppress

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from sloperator.admin import create_admin_routes
from sloperator.agents import AgentOrchestrator, validate_agent_runtime
from sloperator.archive import periodically_synchronize_archive, synchronize_archive
from sloperator.automation_controls import AutomationControls
from sloperator.bot import create_app
from sloperator.config import ConfigurationError, Settings
from sloperator.experiment_finalizer import cancel_task, publish_run, run_daily
from sloperator.health import create_health_app
from sloperator.store import EventStore
from sloperator.subscription_flow import (
    SubscriptionFlowResponder,
    is_subscription_flow_event,
)
from sloperator.vpn import VpnError, VpnManager, VpnState

LOGGER = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """Configure concise structured-enough server logs."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def serve(settings: Settings) -> None:
    """Run Socket Mode and the health endpoint until termination."""
    validate_agent_runtime(settings)
    store = EventStore(settings.database_path)
    await asyncio.to_thread(store.initialize)
    recovered = await asyncio.to_thread(store.recover_interrupted_agent_work)
    if recovered:
        LOGGER.warning("Recovered %d interrupted agent session(s)", recovered)
    vpn = VpnManager(settings)
    orchestrator = AgentOrchestrator(settings, store, vpn)
    automation_controls = AutomationControls(
        settings.database_path.parent / "automation-controls.json"
    )
    subscription_flow_responder = SubscriptionFlowResponder(settings, store, orchestrator)
    app = create_app(
        settings,
        store,
        orchestrator,
        vpn,
        subscription_flow_responder=subscription_flow_responder,
        automation_controls=automation_controls,
    )

    async def handle_new_history_message(
        channel_id: str,
        message: Mapping[str, object],
    ) -> None:
        event = {**message, "channel": channel_id}
        if not automation_controls.disabled(
            "triggers", "subscription-flow"
        ) and is_subscription_flow_event(event, settings):
            await subscription_flow_responder.handle(event, app.client)

    slack_handler = AsyncSocketModeHandler(app, settings.app_token)
    http_app = create_health_app()
    create_admin_routes(http_app, store, orchestrator, app.client, automation_controls)
    runner = web.AppRunner(http_app, access_log=None)
    stop_event = asyncio.Event()
    archive_task: asyncio.Task[None] | None = None
    vpn_task: asyncio.Task[None] | None = None
    experiment_finalizer_task: asyncio.Task[None] | None = None
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)

    try:
        await site.start()
        await slack_handler.connect_async()  # type: ignore[no-untyped-call]
        await orchestrator.resume_interrupted(app.client)
        recovered_headless = await orchestrator.resume_interrupted_headless(
            settings.experiment_finalizer_timeout_seconds
        )
        for run in recovered_headless:
            await publish_run(app.client, orchestrator, settings, run)
            if run.run_id is not None:
                await asyncio.to_thread(
                    store.finish_scheduled_agent_run,
                    run.run_id,
                    status="completed",
                    external_session_id=run.session_id,
                    result_text=run.text,
                )
        await synchronize_archive(app.client, store, settings.backfill_limit)
        archive_task = asyncio.create_task(
            periodically_synchronize_archive(
                app.client,
                store,
                settings.backfill_limit,
                settings.sync_interval_seconds,
                on_new_message=handle_new_history_message,
            ),
            name="slack-archive-sync",
        )
        if vpn.configured:
            vpn_task = asyncio.create_task(
                _monitor_vpn(app, settings, vpn),
                name="vpn-monitor",
            )
        if settings.experiment_finalizer_enabled:
            experiment_finalizer_task = asyncio.create_task(
                run_daily(
                    app.client,
                    orchestrator,
                    settings,
                    lambda: (
                        not automation_controls.disabled(
                            "crons", "experiment-finalizer (sloperator.service)"
                        )
                    ),
                ),
                name="daily-experiment-finalizer",
            )
        LOGGER.info(
            "Sloperator started; health http://%s:%d/healthz; admin /admin; archive %s",
            settings.host,
            settings.port,
            store.path,
        )
        await stop_event.wait()
    finally:
        LOGGER.info("Sloperator is shutting down")
        if archive_task is not None:
            archive_task.cancel()
            with suppress(asyncio.CancelledError):
                await archive_task
        if vpn_task is not None:
            vpn_task.cancel()
            with suppress(asyncio.CancelledError):
                await vpn_task
        await cancel_task(experiment_finalizer_task)
        await orchestrator.close()
        await slack_handler.close_async()  # type: ignore[no-untyped-call]
        await runner.cleanup()


async def _monitor_vpn(
    app: AsyncApp,
    settings: Settings,
    vpn: VpnManager,
) -> None:
    """Report unavailable VPN and wait for explicit owner readiness."""
    client = app.client
    ready_notice_sent = False
    waiting_notice_sent = False

    while True:
        try:
            state = await vpn.state()

            if state in {VpnState.STOPPED, VpnState.FAILED} and not ready_notice_sent:
                message = (
                    "VPN сейчас не подключён. Когда будешь готов сразу прислать "
                    "одноразовый код, напиши `vpn ready` или `готов`. "
                    "Только после этого я начну подключение."
                )
                conversation = await client.conversations_open(users=settings.slack_user_id)
                await client.chat_postMessage(
                    channel=conversation["channel"]["id"],
                    text=message,
                )
                ready_notice_sent = True
            elif state is VpnState.WAITING_OTP and not waiting_notice_sent:
                message = (
                    "VPN запущен, LDAP принят. Нужен одноразовый код: "
                    "пришлите сюда 6-8 цифр отдельным сообщением."
                )
                conversation = await client.conversations_open(users=settings.slack_user_id)
                await client.chat_postMessage(
                    channel=conversation["channel"]["id"],
                    text=message,
                )
                waiting_notice_sent = True
            elif state is VpnState.CONNECTED:
                ready_notice_sent = False
                waiting_notice_sent = False
        except VpnError as error:
            LOGGER.error("VPN monitoring failed: %s", type(error).__name__)

        await asyncio.sleep(60)


def run() -> None:
    """CLI entry point."""
    try:
        settings = Settings.from_environment()
    except ConfigurationError as error:
        raise SystemExit(f"Configuration error: {error}") from error

    configure_logging(settings.log_level)
    with suppress(KeyboardInterrupt):
        asyncio.run(serve(settings))


if __name__ == "__main__":
    run()
