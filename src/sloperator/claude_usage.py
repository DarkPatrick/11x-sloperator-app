"""Read Claude Code subscription usage for scheduler admission decisions."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path


USAGE_RE = re.compile(
    r"Current session:\s*(?P<session>\d+)% used.*?resets (?P<session_reset>[^\n]+)\n"
    r"Current week \(all models\):\s*(?P<week>\d+)% used.*?resets (?P<week_reset>[^\n]+)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ClaudeUsage:
    session_used_percent: int
    week_used_percent: int
    session_reset_text: str
    week_reset_text: str

    @property
    def session_remaining_percent(self) -> int:
        return 100 - self.session_used_percent

    @property
    def week_remaining_percent(self) -> int:
        return 100 - self.week_used_percent


class ClaudeUsageError(RuntimeError):
    """Raised when Claude does not provide a trustworthy usage report."""


def parse_usage(text: str) -> ClaudeUsage:
    match = USAGE_RE.search(text)
    if match is None:
        raise ClaudeUsageError("Claude /usage output has no session and weekly limits")
    session = int(match.group("session"))
    week = int(match.group("week"))
    if not 0 <= session <= 100 or not 0 <= week <= 100:
        raise ClaudeUsageError("Claude /usage returned an invalid percentage")
    return ClaudeUsage(session, week, match.group("session_reset").strip(), match.group("week_reset").strip())


async def read_usage(cli: Path, *, model: str = "opus", cwd: Path = Path("/tmp")) -> ClaudeUsage:
    process = await asyncio.create_subprocess_exec(
        str(cli), "-p", "--model", model, "--output-format", "json", "/usage",
        cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    if process.returncode != 0:
        raise ClaudeUsageError(f"Claude /usage failed: {stderr.decode(errors='replace')[-500:]}")
    try:
        payload = json.loads(stdout)
        text = payload["result"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ClaudeUsageError("Claude /usage returned malformed JSON") from error
    return parse_usage(text)
