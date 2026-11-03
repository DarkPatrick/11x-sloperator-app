"""Reduced instruction profiles for Claude runs of selected workflows.

The agent workspace (ug-ai-analyst) owns the profiles: ``.claude/profiles/build_profile.py`` turns
a profile name into a text file plus Claude settings that hide the full CLAUDE.md and the unused
project skills. A profile is named after the workflow that uses it. Any failure falls back to the
full context, because a run with too much context is slower and dearer but still correct.
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


def claude_profile_options(settings: Settings, workflow: str) -> tuple[str, ...]:
    """Return the extra ``claude`` options for ``workflow``, or () to keep the full context."""
    if workflow not in settings.claude_profile_workflows:
        return ()
    workspace = settings.agent_workspace
    builder = workspace / BUILDER
    python = workspace / ".venv" / "bin" / "python"
    command = [
        str(python if python.exists() else "python3"),
        "-I",
        str(builder),
        "build",
        workflow,
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
            "Claude profile %s unavailable, running with full context: %s", workflow, detail
        )
        return ()
    return ("--settings", claude_settings, "--append-system-prompt-file", str(prompt_file))
