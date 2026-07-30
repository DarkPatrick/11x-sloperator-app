"""Session-scoped SQL completion agents for the local admin editor."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sloperator.agents import (
    ActiveAgentRun,
    run_claude,
    run_codex,
)
from sloperator.config import Settings
from sloperator.store import AgentSession, EventStore

SQL_INITIAL_INSTRUCTION = """\
You are a SQL-only completion assistant for an Ultimate Guitar product analyst.
Work in the current ug-ai-analyst repository. This editor session is intentionally
lightweight: do not run freshness_preflight.sh, any session/tool hooks, skills, plugins,
MCP servers, or repository automation. UG_SKIP_PREFLIGHT=1 is deliberate for this flow.
Before the first completion, read:
- context/rules/sql-style.md
- context/ultimate-guitar-product-context.md
- the relevant SQL/data-warehouse context, including clickhouse-worker documentation
- ug-experiment-calculator source-of-truth queries and metric definitions whenever the
  draft concerns experiments, subscriptions, trials, payments, revenue, ARPU, or LTV.

The user is actively writing SQL in the left pane of an editor. Infer what they are
trying to write next from the complete draft, including all comments. Return the most
useful continuation or corrected/completed query.

Only help with SQL. Do not execute queries, calculate results, explain the answer, ask
questions, create reports, or package artifacts. Your entire final response must be
copyable SQL text with SQL comments allowed. Do not use Markdown fences or prose outside
SQL comments.

Current editor draft:
"""

SQL_FOLLOWUP_INSTRUCTION = """\
The editor draft changed. Infer the intended next SQL and return only copyable SQL
(no Markdown fences or prose). Treat this full draft as the current source of truth:

"""


@dataclass(slots=True)
class _SqlSession:
    provider: str
    model: str
    external_session_id: str | None = None
    turn_count: int = 0


class AdminSqlManager:
    """Keep lightweight provider sessions isolated by browser-generated IDs."""

    def __init__(self, settings: Settings, store: EventStore) -> None:
        self.settings = settings
        self.store = store
        self._sessions: dict[str, _SqlSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._controls: dict[str, ActiveAgentRun] = {}

    async def complete(self, session_id: str, provider: str, sql: str) -> str:
        """Generate one SQL completion while preserving provider conversation context."""
        if provider not in {"claude", "codex"}:
            raise ValueError("Unknown SQL agent provider")
        if not sql.strip():
            raise ValueError("SQL draft is required")

        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            model = (
                self.settings.claude_model
                if provider == "claude"
                else self.settings.codex_model
            )
            state = self._sessions.get(session_id)
            if state is None or state.provider != provider:
                state = _SqlSession(provider=provider, model=model)
                self._sessions[session_id] = state

            agent_session = AgentSession(
                channel_id="admin-sql",
                thread_ts=session_id,
                provider=provider,
                model=state.model,
                external_session_id=state.external_session_id,
                status="running",
                turn_count=state.turn_count,
                last_error=None,
            )
            control = ActiveAgentRun(provider)
            self._controls[session_id] = control
            prompt = (
                SQL_INITIAL_INSTRUCTION + sql
                if state.turn_count == 0
                else SQL_FOLLOWUP_INSTRUCTION + sql
            )
            try:
                if provider == "claude":
                    result = await run_claude(
                        self.settings,
                        agent_session,
                        prompt,
                        control,
                        environment_overrides={"UG_SKIP_PREFLIGHT": "1"},
                        initial_instruction="",
                        command_options=("--safe-mode",),
                    )
                else:
                    result = await run_codex(
                        self.settings,
                        agent_session,
                        prompt,
                        control,
                        self.store,
                        environment_overrides={"UG_SKIP_PREFLIGHT": "1"},
                        initial_instruction="",
                    )
            finally:
                self._controls.pop(session_id, None)

            state.external_session_id = result.session_id
            state.turn_count += 1
            return _strip_markdown_fence(result.text)

    async def close(self) -> None:
        """Cancel active completions during application shutdown."""
        controls = list(self._controls.values())
        await asyncio.gather(
            *(control.steer("stop") for control in controls),
            return_exceptions=True,
        )


def _strip_markdown_fence(text: str) -> str:
    """Defensively unwrap a single SQL fence despite the output instruction."""
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return value
