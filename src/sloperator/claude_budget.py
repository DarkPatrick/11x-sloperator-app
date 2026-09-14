"""Bound automated Claude sessions using local usage, including child agents."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path


class ClaudeBudgetExceeded(RuntimeError):
    """Terminal spending stop; never retry or launch a recovery agent."""


class ClaudeQuotaExceeded(RuntimeError):
    """The provider has rejected work because its usage allowance is exhausted."""


class TranscriptBudget:
    def __init__(
        self,
        directory: Path,
        session_id: str,
        *,
        input_limit: int,
        output_limit: int,
    ) -> None:
        self.directory = directory
        self.session_id = session_id
        self.input_limit = input_limit
        self.output_limit = output_limit
        self.offsets: dict[Path, int] = {}
        self.usage: dict[tuple[Path, str], tuple[int, int]] = {}

    def check(self) -> None:
        paths = [self.directory / f"{self.session_id}.jsonl"]
        paths.extend((self.directory / self.session_id / "subagents").glob("**/*.jsonl"))
        for path in paths:
            if not path.exists():
                continue
            offset = self.offsets.get(path, 0)
            # Keep previous usage if a provider rewrites a transcript: restarting must
            # never refund already consumed budget.
            if path.stat().st_size < offset:
                offset = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        break  # Provider is still writing this record.
                    self.offsets[path] = stream.tell()
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if record.get("type") != "assistant":
                        continue
                    message = record.get("message", {})
                    usage = message.get("usage") or {}
                    if not message.get("id") or not usage:
                        continue
                    inputs = sum(
                        usage.get(k, 0)
                        for k in (
                            "input_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                        )
                    )
                    outputs = usage.get("output_tokens", 0)
                    key = (path, message["id"])
                    old = self.usage.get(key, (0, 0))
                    self.usage[key] = (max(old[0], inputs), max(old[1], outputs))
        inputs = sum(item[0] for item in self.usage.values())
        outputs = sum(item[1] for item in self.usage.values())
        if inputs >= self.input_limit or outputs >= self.output_limit:
            raise ClaudeBudgetExceeded(
                f"Claude session budget reached: input={inputs}/{self.input_limit}, "
                f"output={outputs}/{self.output_limit} (including subagents)"
            )

    async def run[T](self, operation: Callable[[], Awaitable[T]], *, interval: float = 2) -> T:
        await asyncio.to_thread(self.check)
        task = asyncio.create_task(operation())
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=interval)
                await asyncio.to_thread(self.check)
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def transcript_directory(workspace: Path) -> Path:
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    return root / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(workspace.resolve()))


def quota_exhausted(text: str) -> bool:
    normalized = text.casefold()
    return any(
        marker in normalized
        for marker in (
            "you've hit your session limit",
            "you've hit your weekly limit",
            "you've hit your usage limit",
            "usage limit reached",
        )
    )
