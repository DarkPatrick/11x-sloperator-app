"""Exclusive lock shared by every agent process working in one workspace."""

from __future__ import annotations

import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCK_DIRECTORY = PROJECT_ROOT / "data" / "locks"
FLOCK = "/usr/bin/flock"


def workspace_lock_path(workspace: Path, directory: Path = LOCK_DIRECTORY) -> Path:
    """Return the lock file for a workspace, kept in Sloperator's own writable data directory.

    The lock used to live in the workspace's `.git`, which the hardened service unit mounts
    read-only for any workspace other than the agent checkout, so `flock` could not create it.
    """
    resolved = str(workspace.resolve())
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:12]
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{workspace.name or 'root'}-{digest}.lock"


def with_workspace_lock(
    workspace: Path, command: list[str], directory: Path = LOCK_DIRECTORY
) -> list[str]:
    """Serialize agents that share one working tree."""
    return [FLOCK, "-x", str(workspace_lock_path(workspace, directory)), *command]


def is_lock_failure(stderr: str) -> bool:
    """Recognize `flock` failing before the agent CLI ever started; retrying cannot help."""
    return stderr.lstrip().startswith("flock:")
