"""Keep agent scratch files off the memory-backed /tmp and remove stale ones.

The service runs with ``PrivateTmp=true`` on a host whose /tmp is tmpfs, so every
byte agents leave in /tmp (repository clones, build output) is charged to the
service's ``MemoryMax``.  Agents inherit a disk-backed ``TMPDIR`` instead, and an
hourly sweep deletes top-level scratch entries nobody has touched for hours, both
in that directory and in the private /tmp for agents that hardcode ``/tmp``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 6 * 3600
SWEEP_INTERVAL_SECONDS = 3600


def configure_disk_tmp(path: Path) -> Path:
    """Point this process and every child at a disk-backed temporary directory."""
    path = path.resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(path)
    os.environ["CLAUDE_CODE_TMPDIR"] = str(path)
    import tempfile

    tempfile.tempdir = None  # re-read TMPDIR on next use
    return path


def _newest_mtime(entry: Path) -> float:
    newest = entry.lstat().st_mtime
    if entry.is_dir() and not entry.is_symlink():
        for root, dirs, files in os.walk(entry):
            for name in (*dirs, *files):
                try:
                    newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
                except OSError:
                    continue
    return newest


def sweep(roots: list[Path], *, stale_after: float = STALE_AFTER_SECONDS) -> int:
    """Delete own top-level entries under ``roots`` untouched for ``stale_after``."""
    cutoff = time.time() - stale_after
    uid = os.getuid()
    removed = 0
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.lstat().st_uid != uid or _newest_mtime(entry) >= cutoff:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError as error:
                LOGGER.warning("Could not remove stale scratch entry %s: %s", entry, error)
                continue
            removed += 1
            LOGGER.info("Removed stale scratch entry %s", entry)
    return removed


async def run_periodic_sweep(roots: list[Path]) -> None:
    """Sweep stale scratch entries now and then every hour."""
    while True:
        try:
            await asyncio.to_thread(sweep, roots)
        except Exception:
            LOGGER.exception("Scratch sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
