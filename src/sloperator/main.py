"""Application lifecycle."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from sloperator.bot import create_app
from sloperator.config import ConfigurationError, Settings
from sloperator.health import create_health_app

LOGGER = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """Configure concise structured-enough server logs."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def serve(settings: Settings) -> None:
    """Run Socket Mode and the health endpoint until termination."""
    slack_handler = AsyncSocketModeHandler(create_app(settings), settings.app_token)
    runner = web.AppRunner(create_health_app(), access_log=None)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)

    try:
        await site.start()
        await slack_handler.connect_async()  # type: ignore[no-untyped-call]
        LOGGER.info(
            "Sloperator started; health endpoint listening on http://%s:%d/healthz",
            settings.host,
            settings.port,
        )
        await stop_event.wait()
    finally:
        LOGGER.info("Sloperator is shutting down")
        await slack_handler.close_async()  # type: ignore[no-untyped-call]
        await runner.cleanup()


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
