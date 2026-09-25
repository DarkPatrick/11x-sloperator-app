"""Daily ug-ai-analyst skill documentation synchronization."""

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

LOGGER = logging.getLogger(__name__)
TIMEZONE = dt.UTC
TIMEZONE_NAME = "UTC"
HOUR = 0
TIMEOUT_SECONDS = 14_400
PROMPT_SOURCE = "ug-ai-analyst scripts/skill_docs_prompt.md"
# Printed by the repository sync when it stops on a spent Claude allowance (its EX_TEMPFAIL path).
QUOTA_SENTINEL = "SKILL_DOCS_QUOTA_EXHAUSTED"
PROMPT = f"""\
{AUTOMATED_RESPONSE_STYLE}

The skill documentation sync appends this policy to the repository-owned skill documentation
prompt, then asks Claude to return the strict summary and Confluence storage XHTML blocks defined
there. The prompt is populated separately for every stale skill from its tracked contract,
references, and change history.
"""


class SkillDocsSyncError(RuntimeError):
    """The repository refresh or documentation synchronization failed."""


def next_run_at(now: dt.datetime) -> dt.datetime:
    """Return the next midnight UTC."""
    utc_now = now.astimezone(dt.UTC)
    candidate = utc_now.replace(hour=HOUR, minute=0, second=0, microsecond=0)
    if candidate <= utc_now:
        candidate += dt.timedelta(days=1)
    return candidate


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
        # The sync stops itself on a spent allowance and reports it verbatim; hand it to the
        # shared quota wait instead of burning the day's attempt on a failure that only time
        # fixes. The whole output is searched: the refusal is logged before the exit summary.
        if QUOTA_SENTINEL in output or quota_exhausted(output):
            raise ClaudeQuotaExceeded(f"skill docs sync stopped on the Claude usage limit: {tail}")
        raise SkillDocsSyncError(
            f"{' '.join(command[:3])} exited {process.returncode}: {tail}"
        )
    return output


async def run_once(settings: Settings) -> str:
    """Fast-forward ug-ai-analyst and publish documentation for stale skills."""
    workspace = settings.agent_workspace.resolve()
    preflight = workspace / "scripts" / "freshness_preflight.sh"
    sync_script = workspace / "scripts" / "skill_docs_sync.py"
    python = workspace / ".venv" / "bin" / "python"
    if not preflight.is_file():
        raise SkillDocsSyncError(f"Repository freshness preflight is missing in {workspace}")
    if not sync_script.is_file():
        raise SkillDocsSyncError(f"Skill docs sync script is missing in {workspace}")
    if not python.is_file():
        raise SkillDocsSyncError(f"Repository virtualenv Python is missing in {workspace}")

    await _run_command(
        (str(preflight),),
        cwd=workspace,
    )
    env = {
        **os.environ,
        "CLAUDE_BIN": str(settings.claude_cli),
        "CLAUDE_MODEL": settings.claude_model,
        "UG_SKIP_PREFLIGHT": "1",
        "SKILL_DOCS_PROMPT_PREAMBLE": AUTOMATED_RESPONSE_STYLE,
    }
    return await _run_command(
        (str(python), str(sync_script), "sync"),
        cwd=workspace,
        env=env,
    )


async def run_daily(
    settings: Settings,
    enabled: Callable[[], bool] = lambda: True,
) -> None:
    """Run forever once per calendar day at midnight UTC."""
    while True:
        now = dt.datetime.now(dt.UTC)
        target = next_run_at(now)
        LOGGER.info("Next skill docs sync scheduled for %s", target.isoformat())
        await asyncio.sleep((target - now).total_seconds())
        if not enabled():
            LOGGER.info("Scheduled skill docs sync disabled from admin")
            continue
        try:
            LOGGER.info("Starting scheduled skill docs sync")
            output = await retry_claude_quota(
                lambda: run_once(settings),
                settings,
                context="Scheduled skill docs sync",
            )
            if output:
                LOGGER.info("Skill docs sync output: %s", output[-2_000:])
            LOGGER.info("Skill docs sync completed")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Daily skill docs sync failed")


async def cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
