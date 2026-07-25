"""Application lifecycle."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from sloperator.agents import AgentOrchestrator, validate_agent_runtime
from sloperator.archive import periodically_synchronize_archive, synchronize_archive
from sloperator.bot import create_app
from sloperator.config import ConfigurationError, Settings
from sloperator.health import create_health_app
from sloperator.store import EventStore
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
    app = create_app(settings, store, orchestrator, vpn)
    slack_handler = AsyncSocketModeHandler(app, settings.app_token)
    runner = web.AppRunner(create_health_app(), access_log=None)
    stop_event = asyncio.Event()
    archive_task: asyncio.Task[None] | None = None
    vpn_task: asyncio.Task[None] | None = None
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)

    try:
        await site.start()
        await slack_handler.connect_async()  # type: ignore[no-untyped-call]
        await synchronize_archive(app.client, store, settings.backfill_limit)
        archive_task = asyncio.create_task(
            periodically_synchronize_archive(
                app.client,
                store,
                settings.backfill_limit,
                settings.sync_interval_seconds,
            ),
            name="slack-archive-sync",
        )
        if vpn.configured:
            vpn_task = asyncio.create_task(
                _monitor_vpn(app, settings, vpn),
                name="vpn-monitor",
            )
        LOGGER.info(
            "Sloperator started; health endpoint http://%s:%d/healthz; archive %s",
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
        await orchestrator.close()
        await slack_handler.close_async()  # type: ignore[no-untyped-call]
        await runner.cleanup()


async def _monitor_vpn(
    app: AsyncApp,
    settings: Settings,
    vpn: VpnManager,
) -> None:
    """Keep VPN available and ask the owner for OTP only when necessary."""
    client = app.client
    waiting_notice_sent = False
    error_notice_sent = False

    while True:
        try:
            state = await vpn.state()
            if state in {VpnState.STOPPED, VpnState.FAILED}:
                state = await vpn.connect()

            if state is VpnState.WAITING_OTP and not waiting_notice_sent:
                message = (
                    "VPN запущен, LDAP принят. Нужен одноразовый код: "
                    "пришлите сюда 6-8 цифр отдельным сообщением."
                )
                conversation = await client.conversations_open(
                    users=settings.slack_user_id
                )
                await client.chat_postMessage(
                    channel=conversation["channel"]["id"],
                    text=message,
                )
                waiting_notice_sent = True
            elif state is VpnState.CONNECTED:
                waiting_notice_sent = False

            error_notice_sent = False
        except VpnError as error:
            LOGGER.error("Automatic VPN connection failed: %s", type(error).__name__)
            if not error_notice_sent:
                conversation = await client.conversations_open(
                    users=settings.slack_user_id
                )
                await client.chat_postMessage(
                    channel=conversation["channel"]["id"],
                    text=f"Automatic VPN connection failed: {error}",
                )
                error_notice_sent = True

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
