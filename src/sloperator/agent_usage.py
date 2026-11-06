"""Per-invocation agent token accounting and stable logical-agent names."""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentUsageContext:
    agent_name: str
    workflow: str
    role: str
    source: str
    source_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    provider_turns: int | None = None

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
            + self.output_tokens
        )

    def minus(self, earlier: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=max(0, self.input_tokens - earlier.input_tokens),
            cache_creation_input_tokens=max(
                0, self.cache_creation_input_tokens - earlier.cache_creation_input_tokens
            ),
            cache_read_input_tokens=max(
                0, self.cache_read_input_tokens - earlier.cache_read_input_tokens
            ),
            output_tokens=max(0, self.output_tokens - earlier.output_tokens),
        )


def context_for_job(job_name: str, *, source_run_id: str | None = None) -> AgentUsageContext:
    """Split existing scheduler names into stable workflow/role dimensions."""
    role_aliases = {"preparer": "worker"}
    for suffix in ("reviewer", "preparer", "worker", "precheck"):
        marker = f"-{suffix}"
        if job_name.endswith(marker):
            workflow = job_name[: -len(marker)]
            role = role_aliases.get(suffix, suffix)
            return AgentUsageContext(
                f"{workflow}/{role}", workflow, role, "scheduled", source_run_id
            )
    return AgentUsageContext(
        f"{job_name}/agent", job_name, "agent", "scheduled", source_run_id
    )


def slack_context(agent_name: str | None, *, source_run_id: str | None = None) -> AgentUsageContext:
    if agent_name:
        workflow, _, role = agent_name.partition("/")
        return AgentUsageContext(
            agent_name, workflow or "slack", role or "slack", "slack", source_run_id
        )
    return AgentUsageContext("slack/general", "slack", "general", "slack", source_run_id)


def slack_agent_name(job_name: str | None) -> str:
    if not job_name:
        return "slack/general"
    scheduled = context_for_job(job_name)
    return f"{scheduled.workflow}/slack"


def parse_claude_usage(payload: Mapping[str, Any]) -> TokenUsage | None:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    parsed = TokenUsage(
        input_tokens=_integer(usage.get("input_tokens")),
        cache_creation_input_tokens=_integer(usage.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_integer(usage.get("cache_read_input_tokens")),
        output_tokens=_integer(usage.get("output_tokens")),
        cost_usd=_number(payload.get("total_cost_usd")),
        provider_turns=_optional_integer(payload.get("num_turns")),
    )
    return parsed if parsed.total_tokens else None


def parse_codex_usage(turn: Mapping[str, Any]) -> TokenUsage | None:
    usage = turn.get("usage") or turn.get("tokenUsage") or turn.get("token_usage")
    if not isinstance(usage, Mapping):
        return None
    parsed = TokenUsage(
        input_tokens=_integer(_first(usage, "inputTokens", "input_tokens")),
        cache_read_input_tokens=_integer(
            _first(usage, "cachedInputTokens", "cached_input_tokens", "cache_read_input_tokens")
        ),
        output_tokens=_integer(_first(usage, "outputTokens", "output_tokens")),
    )
    return parsed if parsed.total_tokens else None


def transcript_usage(directory: Path, session_id: str) -> TokenUsage:
    """Read cumulative Claude usage, including child-agent transcripts."""
    paths = [directory / f"{session_id}.jsonl"]
    paths.extend((directory / session_id / "subagents").glob("**/*.jsonl"))
    messages: dict[tuple[Path, str], TokenUsage] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") != "assistant":
                continue
            message = record.get("message") or {}
            usage = message.get("usage") or {}
            identifier = message.get("id")
            if not identifier or not isinstance(usage, Mapping):
                continue
            current = TokenUsage(
                input_tokens=_integer(usage.get("input_tokens")),
                cache_creation_input_tokens=_integer(usage.get("cache_creation_input_tokens")),
                cache_read_input_tokens=_integer(usage.get("cache_read_input_tokens")),
                output_tokens=_integer(usage.get("output_tokens")),
            )
            previous = messages.get((path, str(identifier)), TokenUsage())
            messages[(path, str(identifier))] = TokenUsage(
                input_tokens=max(previous.input_tokens, current.input_tokens),
                cache_creation_input_tokens=max(
                    previous.cache_creation_input_tokens, current.cache_creation_input_tokens
                ),
                cache_read_input_tokens=max(
                    previous.cache_read_input_tokens, current.cache_read_input_tokens
                ),
                output_tokens=max(previous.output_tokens, current.output_tokens),
            )
    return TokenUsage(
        input_tokens=sum(item.input_tokens for item in messages.values()),
        cache_creation_input_tokens=sum(
            item.cache_creation_input_tokens for item in messages.values()
        ),
        cache_read_input_tokens=sum(item.cache_read_input_tokens for item in messages.values()),
        output_tokens=sum(item.output_tokens for item in messages.values()),
    )


@dataclass(frozen=True, slots=True)
class TranscriptActivity:
    """Tool calls seen in a Claude session, including child agents, keyed by tool_use id."""

    tool_uses: Mapping[str, tuple[str, str | None]] = field(default_factory=dict)
    subagent_transcripts: frozenset[str] = frozenset()
    subagent_usage: TokenUsage = TokenUsage()


@dataclass(frozen=True, slots=True)
class ActivityDelta:
    """What one provider invocation added to a session: tools, skills and child agents."""

    tool_calls: int = 0
    tool_counts: Mapping[str, int] = field(default_factory=dict)
    skills: tuple[str, ...] = ()
    subagents: int = 0
    subagent_tokens: int = 0


def transcript_activity(directory: Path, session_id: str) -> TranscriptActivity:
    """Read every tool call of a session and its child agents from the local transcripts."""
    if not session_id:
        return TranscriptActivity()
    main = directory / f"{session_id}.jsonl"
    children = sorted((directory / session_id / "subagents").glob("**/*.jsonl"))
    tool_uses: dict[str, tuple[str, str | None]] = {}
    for path in [main, *children]:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") != "assistant":
                continue
            content = (record.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                identifier = block.get("id")
                name = block.get("name")
                if not identifier or not isinstance(name, str):
                    continue
                arguments = block.get("input")
                skill = arguments.get("skill") if name == "Skill" and isinstance(
                    arguments, Mapping
                ) else None
                tool_uses[str(identifier)] = (name, skill if isinstance(skill, str) else None)
    return TranscriptActivity(
        tool_uses=tool_uses,
        subagent_transcripts=frozenset(str(path) for path in children),
        subagent_usage=_children_usage(directory, session_id) if children else TokenUsage(),
    )


def activity_delta(before: TranscriptActivity, after: TranscriptActivity) -> ActivityDelta:
    """Tool calls, skills and child agents that appeared between two snapshots."""
    new = [value for key, value in after.tool_uses.items() if key not in before.tool_uses]
    counts = Counter(name for name, _ in new)
    skills = tuple(sorted({skill for _, skill in new if skill}))
    return ActivityDelta(
        tool_calls=len(new),
        tool_counts=dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
        skills=skills,
        subagents=len(after.subagent_transcripts - before.subagent_transcripts),
        subagent_tokens=after.subagent_usage.minus(before.subagent_usage).total_tokens,
    )


@lru_cache(maxsize=1)
def own_commit() -> str | None:
    """The sloperator checkout commit this process was started from (read once)."""
    return git_commit(Path(__file__).resolve().parent)


def git_commit(path: Path) -> str | None:
    """Short HEAD commit of the checkout holding ``path``, or None when unknown."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def _children_usage(directory: Path, session_id: str) -> TokenUsage:
    """Usage of child agents only: the session total minus the main transcript."""
    total = transcript_usage(directory, session_id)
    main_only = _main_usage(directory / f"{session_id}.jsonl")
    return total.minus(main_only)


def _main_usage(path: Path) -> TokenUsage:
    if not path.is_file():
        return TokenUsage()
    messages: dict[str, TokenUsage] = {}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return TokenUsage()
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        usage = message.get("usage") or {}
        identifier = message.get("id")
        if not identifier or not isinstance(usage, Mapping):
            continue
        messages[str(identifier)] = _max_usage(messages.get(str(identifier), TokenUsage()), usage)
    return TokenUsage(
        input_tokens=sum(item.input_tokens for item in messages.values()),
        cache_creation_input_tokens=sum(
            item.cache_creation_input_tokens for item in messages.values()
        ),
        cache_read_input_tokens=sum(item.cache_read_input_tokens for item in messages.values()),
        output_tokens=sum(item.output_tokens for item in messages.values()),
    )


def _max_usage(previous: TokenUsage, usage: Mapping[str, Any]) -> TokenUsage:
    return TokenUsage(
        input_tokens=max(previous.input_tokens, _integer(usage.get("input_tokens"))),
        cache_creation_input_tokens=max(
            previous.cache_creation_input_tokens,
            _integer(usage.get("cache_creation_input_tokens")),
        ),
        cache_read_input_tokens=max(
            previous.cache_read_input_tokens, _integer(usage.get("cache_read_input_tokens"))
        ),
        output_tokens=max(previous.output_tokens, _integer(usage.get("output_tokens"))),
    )


def fallback_context(channel_id: str, thread_ts: str) -> AgentUsageContext:
    safe_channel = re.sub(r"[^a-z0-9-]+", "-", channel_id.casefold()).strip("-")
    return AgentUsageContext(
        f"{safe_channel or 'agent'}/general",
        safe_channel or "agent",
        "general",
        "direct",
        thread_ts,
    )


def _first(values: Mapping[str, Any], *keys: str) -> Any:
    return next((values[key] for key in keys if key in values), 0)


def _integer(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _optional_integer(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
