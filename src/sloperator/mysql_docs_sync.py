"""Weekly upkeep of the ug-ai-analyst MySQL table documentation (Confluence UGuide → MySQL)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

from sloperator.agents import retry_claude_quota
from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.claude_budget import ClaudeQuotaExceeded, quota_exhausted
from sloperator.config import Settings
from sloperator.scheduler_catchup import SlotRunner

LOGGER = logging.getLogger(__name__)
TIMEZONE = dt.UTC
TIMEZONE_NAME = "UTC"
WEEKDAY = 6  # Sunday
HOUR = 3
TIMEOUT_SECONDS = 14_400
PROMPT_SOURCE = "ug-ai-analyst scripts/mysql_docs/agent_prompt.md"
# Printed by the repository sync when it stops on a spent Claude allowance (its exit-75 path).
QUOTA_SENTINEL = "MYSQL_DOCS_QUOTA_EXHAUSTED"
PROMPT = f"""\
{AUTOMATED_RESPONSE_STYLE}

The MySQL docs sync runs deterministic checks for every documented table (live schema,
commit-pinned backend code links, example queries) and calls a narrow Claude agent (profile
`mysql-docs`, Read/Edit tools only) for at most five tables with drift, with the repository
prompt scripts/mysql_docs/agent_prompt.md plus an evidence packet; it publishes the pages as the
service account and proposes the agent's markdown edits as a pull request.
"""


class MysqlDocsSyncError(RuntimeError):
    """The repository refresh or the documentation sync failed."""


async def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_SECONDS)
    except (asyncio.CancelledError, TimeoutError):
        if process.returncode is None:
            process.terminate()
            with suppress(ProcessLookupError, TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=10)
        raise
    output = stdout.decode(errors="replace").strip()
    if process.returncode != 0:
        tail = output[-2_000:] if output else "no output"
        if QUOTA_SENTINEL in output or quota_exhausted(output):
            raise ClaudeQuotaExceeded(f"MySQL docs sync stopped on the Claude usage limit: {tail}")
        raise MysqlDocsSyncError(f"{' '.join(command[:3])} exited {process.returncode}: {tail}")
    return output


def next_run_at(now: dt.datetime) -> dt.datetime:
    """Return the next Sunday 03:00 UTC strictly after ``now``."""
    utc_now = now.astimezone(dt.UTC)
    candidate = utc_now.replace(hour=HOUR, minute=0, second=0, microsecond=0)
    candidate += dt.timedelta(days=(WEEKDAY - candidate.weekday()) % 7)
    if candidate <= utc_now:
        candidate += dt.timedelta(days=7)
    return candidate


async def run_once(settings: Settings) -> str:
    """Fast-forward ug-ai-analyst and run the weekly MySQL docs sync."""
    workspace = settings.agent_workspace.resolve()
    preflight = workspace / "scripts" / "freshness_preflight.sh"
    sync_script = workspace / "scripts" / "mysql_docs" / "sync.py"
    python = workspace / ".venv" / "bin" / "python"
    required = (
        (preflight, "Repository freshness preflight"),
        (sync_script, "MySQL docs sync script"),
        (python, "Repository virtualenv Python"),
    )
    for path, what in required:
        if not path.is_file():
            raise MysqlDocsSyncError(f"{what} is missing in {workspace}")
    await _run_command((str(preflight),), cwd=workspace)
    env = {
        **os.environ,
        "CLAUDE_BIN": str(settings.claude_cli),
        "CLAUDE_MODEL": settings.claude_model,
        "UG_SKIP_PREFLIGHT": "1",
        "MYSQL_DOCS_PROMPT_PREAMBLE": AUTOMATED_RESPONSE_STYLE,
    }
    return await _run_command((str(python), str(sync_script), "sync"), cwd=workspace, env=env)


async def run_weekly(
    settings: Settings,
    enabled: Callable[[], bool] = lambda: True,
    slots: SlotRunner | None = None,
) -> None:
    """Run forever, once a week on Sunday 03:00 UTC."""
    slots = slots or SlotRunner("mysql-docs-sync")
    while True:
        target = await slots.next_slot(
            lambda now: next_run_at(now), LOGGER, "Next MySQL docs sync scheduled for %s"
        )
        if target is None:
            continue
        if not enabled():
            LOGGER.info("Scheduled MySQL docs sync disabled from admin")
            continue
        try:
            LOGGER.info("Starting scheduled MySQL docs sync")
            output = await retry_claude_quota(
                lambda: run_once(settings),
                settings,
                context="Scheduled MySQL docs sync",
            )
            if output:
                LOGGER.info("MySQL docs sync output: %s", output[-2_000:])
            LOGGER.info("MySQL docs sync completed")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Weekly MySQL docs sync failed")


async def cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
