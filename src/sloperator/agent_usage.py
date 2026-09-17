"""Per-invocation agent token accounting and stable logical-agent names."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
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
