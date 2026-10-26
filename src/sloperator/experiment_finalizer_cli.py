"""One-shot launcher for testing the daily experiment finalizer."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from contextlib import suppress

from sloperator.agents import AgentOrchestrator, validate_agent_runtime
from sloperator.config import ConfigurationError, Settings
from sloperator.experiment_finalizer import (
    START_PROMPT,
    is_finalization_failure,
    overdue_sweep_prompt,
    run_once,
    sweep_overdue_results,
)
from sloperator.main import configure_logging
from sloperator.operations_slack import ObservedSlackClient
from sloperator.operations_store import configure_operations
from sloperator.store import EventStore
from sloperator.vpn import VpnManager


async def launch(settings: Settings, *, sweep_only: bool = False) -> None:
    """Submit one private test run and wait for its agent turn to finish."""
    validate_agent_runtime(settings)
    store = EventStore(settings.database_path)
    await asyncio.to_thread(store.initialize)
    configure_operations(settings.database_path)
    vpn = VpnManager(settings)
    orchestrator = AgentOrchestrator(settings, store, vpn)
    client = ObservedSlackClient(token=settings.bot_token)
    orchestrator.set_notification_client(client)
    try:
        if sweep_only or not is_finalization_failure(
            await run_once(client, orchestrator, settings)
        ):
            await sweep_overdue_results(orchestrator, settings)
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
    parser.add_argument(
        "--sweep-only",
        action="store_true",
        help="run only the overdue Results sweep, without a finalisation run",
    )
    args = parser.parse_args()
    if args.show_prompt:
        print(overdue_sweep_prompt(dt.date.today()) if args.sweep_only else START_PROMPT)
        return
    try:
        settings = Settings.from_environment()
    except ConfigurationError as error:
        raise SystemExit(f"Configuration error: {error}") from error
    configure_logging(settings.log_level)
    with suppress(KeyboardInterrupt):
        asyncio.run(launch(settings, sweep_only=args.sweep_only))


if __name__ == "__main__":
    run()
