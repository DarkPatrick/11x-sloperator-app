"""One-shot launcher for testing the daily experiment finalizer."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import AgentOrchestrator, validate_agent_runtime
from sloperator.config import ConfigurationError, Settings
from sloperator.experiment_finalizer import FINALIZATION_PROMPT, run_once
from sloperator.main import configure_logging
from sloperator.store import EventStore
from sloperator.vpn import VpnManager


async def launch(settings: Settings) -> None:
    """Submit one private test run and wait for its agent turn to finish."""
    validate_agent_runtime(settings)
    store = EventStore(settings.database_path)
    await asyncio.to_thread(store.initialize)
    vpn = VpnManager(settings)
    orchestrator = AgentOrchestrator(settings, store, vpn)
    client = AsyncWebClient(token=settings.bot_token)
    try:
        await run_once(client, orchestrator, settings)
        await orchestrator.drain()
    finally:
        await orchestrator.close()


def run() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="print the initialization prompt without starting an agent",
    )
    args = parser.parse_args()
    if args.show_prompt:
        print(FINALIZATION_PROMPT)
        return
    try:
        settings = Settings.from_environment()
    except ConfigurationError as error:
        raise SystemExit(f"Configuration error: {error}") from error
    configure_logging(settings.log_level)
    with suppress(KeyboardInterrupt):
        asyncio.run(launch(settings))


if __name__ == "__main__":
    run()
