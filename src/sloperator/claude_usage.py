"""Read Claude Code subscription usage for scheduler admission decisions."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)

SESSION_USAGE_RE = re.compile(
    r"^Current session:[ \t]*(?P<session>\d+)%[ \t]+used\b"
    r"(?:[^\n]*?\bresets[ \t]+(?P<reset>[^\n]+))?",
    re.IGNORECASE | re.MULTILINE,
)
WEEK_USAGE_RE = re.compile(
    r"^Current week \(all models\):\s*(?P<week>\d+)%\s+used\b[^\n]*?\bresets\s+(?P<reset>[^\n]+)",
    re.IGNORECASE | re.MULTILINE,
)
RESET_TIME_RE = re.compile(
    r"^(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2}),\s*"
    r"(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*\(UTC\)$",
    re.IGNORECASE,
)
QUOTA_RESET_BUFFER = dt.timedelta(minutes=7)


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

    def __init__(self, message: str, *, diagnostic: str = "") -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


def parse_reset_at(value: str, *, now: dt.datetime) -> dt.datetime | None:
    """Parse Claude's reset label into the next matching UTC datetime."""
    match = RESET_TIME_RE.fullmatch(value.strip())
    if match is None:
        return None
    time_format = "%I:%M%p" if ":" in match.group("time") else "%I%p"
    parsed = dt.datetime.strptime(
        f"{now.year} {match.group('month')} {match.group('day')} {match.group('time')}",
        f"%Y %b %d {time_format}",
    ).replace(tzinfo=dt.UTC)
    if parsed < now - dt.timedelta(days=1):
        parsed = parsed.replace(year=parsed.year + 1)
    return parsed


def quota_retry_at(usage: ClaudeUsage, *, now: dt.datetime) -> dt.datetime:
    """Return a safe retry time after every exhausted Claude allowance resets."""
    resets = [
        (usage.session_used_percent, parse_reset_at(usage.session_reset_text, now=now)),
        (usage.week_used_percent, parse_reset_at(usage.week_reset_text, now=now)),
    ]
    exhausted = [reset for used, reset in resets if used >= 99 and reset is not None]
    available = [reset for _, reset in resets if reset is not None]
    if exhausted:
        reset_at = max(exhausted)
    else:
        future = [reset for reset in available if reset >= now]
        reset_at = min(future) if future else now
    return max(reset_at + QUOTA_RESET_BUFFER, now + dt.timedelta(minutes=5))


def usage_diagnostic(text: str) -> str:
    """Return only quota-related lines, safe to put in an operational alert."""
    lines = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"^Current (?:session|week \(all models\)):", line.strip(), re.IGNORECASE)
    ]
    return " | ".join(lines)[:700] or "no Current session/week quota lines"


def parse_usage(text: str) -> ClaudeUsage:
    session_match = SESSION_USAGE_RE.search(text)
    week_match = WEEK_USAGE_RE.search(text)
    if session_match is None or week_match is None:
        raise ClaudeUsageError("Claude /usage output has no session and weekly limits")
    session = int(session_match.group("session"))
    week = int(week_match.group("week"))
    if not 0 <= session <= 100 or not 0 <= week <= 100:
        raise ClaudeUsageError("Claude /usage returned an invalid percentage")
    return ClaudeUsage(
        session,
        week,
        # An unused session may have no reset time; admission only needs its percentage.
        (session_match.group("reset") or "").strip(),
        week_match.group("reset").strip(),
    )


async def read_usage(
    cli: Path, *, model: str = "claude-opus-5", cwd: Path = Path("/tmp")
) -> ClaudeUsage:
    process = await asyncio.create_subprocess_exec(
        str(cli), "-p", "--model", model, "--output-format", "json", "/usage",
        cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        diagnostic = usage_diagnostic(stderr.decode(errors="replace"))
        error = ClaudeUsageError(
            f"Claude /usage failed: {stderr.decode(errors='replace')[-500:]}",
            diagnostic=diagnostic,
        )
        LOGGER.error("Claude /usage command failed; diagnostic=%s", diagnostic)
        raise error
    try:
        payload = json.loads(stdout)
        text = payload["result"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        diagnostic = usage_diagnostic(stdout.decode(errors="replace"))
        LOGGER.error("Claude /usage returned malformed JSON; diagnostic=%s", diagnostic)
        raise ClaudeUsageError(
            "Claude /usage returned malformed JSON", diagnostic=diagnostic
        ) from error
    try:
        return parse_usage(text)
    except ClaudeUsageError as error:
        diagnostic = usage_diagnostic(text)
        LOGGER.error("Claude /usage format was not recognized; diagnostic=%s", diagnostic)
        raise ClaudeUsageError(str(error), diagnostic=diagnostic) from error
