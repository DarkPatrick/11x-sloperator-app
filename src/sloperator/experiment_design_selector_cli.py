"""Print the deterministic experiment-design candidate as JSON."""

from __future__ import annotations

import asyncio

from sloperator.config import Settings
from sloperator.experiment_design_planner import select_from_jira


async def _run() -> None:
    candidate = await select_from_jira(Settings.from_environment())
    print(candidate.to_json() if candidate is not None else '{"candidate": null}')


def run() -> None:
    asyncio.run(_run())
