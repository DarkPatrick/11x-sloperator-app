"""Reduced instruction profiles for Claude runs of selected workflows.

The agent workspace (ug-ai-analyst) owns the profiles: ``.claude/profiles/build_profile.py`` turns
a key into a text file plus Claude settings that hide the full CLAUDE.md and the unused project
skills. A run is enabled by its agent name or its workflow (``SLOPERATOR_CLAUDE_PROFILE_WORKFLOWS``)
and the workspace maps that key to a profile. Any failure falls back to the default context,
because a run with too much context is slower and dearer but still correct.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from sloperator.config import Settings

LOGGER = logging.getLogger(__name__)
BUILDER = Path(".claude/profiles/build_profile.py")
BUILD_TIMEOUT_SECONDS = 30


def profile_directory(settings: Settings) -> Path:
    return settings.database_path.resolve().parent / "claude-profiles"


def profile_key(settings: Settings, workflow: str, agent_name: str | None = None) -> str | None:
    """Pick the enabled key for a run: its agent name (``workflow/role``) wins over its workflow.

    Listing an agent name enables one role only: ``slack/general`` gets a profile while the gate
    (``slack/communication-gate``), which runs outside the agent workspace, keeps its small context.
    """
    enabled = settings.claude_profile_workflows
    if agent_name and agent_name in enabled:
        return agent_name
    return workflow if workflow in enabled else None


def claude_profile_options(
    settings: Settings, workflow: str, agent_name: str | None = None
) -> tuple[str, ...]:
    """Return the extra ``claude`` options for this run, or () to keep the default context."""
    key = profile_key(settings, workflow, agent_name)
    if key is None:
        return ()
    workspace = settings.agent_workspace
    builder = workspace / BUILDER
    python = workspace / ".venv" / "bin" / "python"
    command = [
        str(python if python.exists() else "python3"),
        "-I",
        str(builder),
        "build",
        key,
        "--out-dir",
        str(profile_directory(settings)),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_SECONDS,
            check=True,
        )
        built = json.loads(completed.stdout)
        prompt_file = Path(built["append_system_prompt_file"])
        if not prompt_file.is_file():
            raise FileNotFoundError(prompt_file)
        claude_settings = json.dumps(built["settings"], sort_keys=True, separators=(",", ":"))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", "") or error
        LOGGER.warning(
            "Claude profile %s unavailable, running with full context: %s", key, detail
        )
        return ()
    return ("--settings", claude_settings, "--append-system-prompt-file", str(prompt_file))
