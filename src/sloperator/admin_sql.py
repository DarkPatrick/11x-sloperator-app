"""Session-scoped SQL completion agents for the local admin editor."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

VIZ_INSTRUCTION = """\
You are building an embedded exploratory visualization for a SQL result.
This session is intentionally lightweight: do not run preflight, hooks, skills, plugins,
MCP servers, queries, or repository automation. Read only these dataviz helper references:
context/dataviz/principles.md, context/dataviz/chart-vocabulary.md,
context/dataviz/chart-refinement.md, and context/dataviz/accessibility.md.

Return one complete self-contained HTML document and nothing else. Do not use Markdown.
Use system fonts and a neutral, accessible palette: do not use UG fonts, brand CSS, or UG
brand colors. Use plain browser JavaScript and inline SVG/HTML only; no network resources.
The document must contain exactly this JavaScript expression where the full result data
will be inserted later:

const payload = __SLOPERATOR_DATA__;

The inserted payload has `columns` and `rows`. Build at most three useful charts based on
the SQL intent, column names/types, and sample. Avoid a chart when a KPI or compact table
is more informative. Make the layout responsive, label axes/units, handle nulls, escape
displayed text, and show a clear empty state. Do not hard-code sample values into charts:
all displayed values must come from `payload`.

SQL:
"""

MAX_QUERY_ROWS = 1_000
QUERY_TIMEOUT_SECONDS = 180
PROHIBITED_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|TRUNCATE|OPTIMIZE|SYSTEM|KILL|"
    r"GRANT|REVOKE|ATTACH|DETACH|RENAME)\b",
    re.IGNORECASE,
)


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

    async def execute(self, sql: str) -> dict[str, Any]:
        """Run one bounded, read-only query through the analyst repository helper."""
        _validate_read_only_sql(sql)
        interpreter = self.settings.agent_workspace / ".venv" / "bin" / "python"
        bridge = Path(__file__).with_name("sql_worker_bridge.py")
        process = await asyncio.create_subprocess_exec(
            str(interpreter),
            str(bridge),
            "--workspace",
            str(self.settings.agent_workspace),
            "--max-rows",
            str(MAX_QUERY_ROWS),
            cwd=str(self.settings.agent_workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(sql.encode()),
                timeout=QUERY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("SQL query exceeded the 180-second timeout") from None
        if process.returncode:
            message = stderr.decode(errors="replace").strip()
            raise RuntimeError(message[-4_000:] or "ClickHouse query failed")
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("ClickHouse helper returned invalid JSON") from error
        if not isinstance(result, dict):
            raise RuntimeError("ClickHouse helper returned an invalid result")
        return result

    async def visualize(
        self,
        session_id: str,
        provider: str,
        sql: str,
        columns: list[object],
        sample_rows: list[object],
    ) -> str:
        """Ask an isolated agent for a data-driven, embeddable HTML visualization."""
        if provider not in {"claude", "codex"}:
            raise ValueError("Unknown visualization agent provider")
        sample = json.dumps(
            {"columns": columns[:100], "rows": sample_rows[:20]},
            ensure_ascii=False,
            default=str,
        )
        model = self.settings.claude_model if provider == "claude" else self.settings.codex_model
        agent_session = AgentSession(
            channel_id="admin-sql-viz",
            thread_ts=session_id,
            provider=provider,
            model=model,
            external_session_id=None,
            status="running",
            turn_count=0,
            last_error=None,
        )
        control = ActiveAgentRun(provider)
        prompt = f"{VIZ_INSTRUCTION}{sql}\n\nColumns and top 20 sample rows:\n{sample}"
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
        html = _strip_markdown_fence(result.text)
        if html.count("__SLOPERATOR_DATA__") != 1:
            raise RuntimeError("Visualization agent returned an invalid data template")
        return html

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


def _validate_read_only_sql(sql: str) -> None:
    """Reject multiple statements and obvious ClickHouse mutations."""
    normalized = re.sub(r"/\*.*?\*/|--[^\n]*", " ", sql, flags=re.DOTALL).strip().strip(";")
    if not normalized:
        raise ValueError("SQL query is required")
    if ";" in normalized:
        raise ValueError("Only one SQL statement is allowed")
    first = re.match(r"[A-Za-z]+", normalized)
    if first is None or first.group(0).upper() not in {"SELECT", "WITH", "EXPLAIN"}:
        raise ValueError("Only read-only SELECT, WITH, or EXPLAIN queries are allowed")
    without_strings = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", " ", normalized)
    if PROHIBITED_SQL_RE.search(without_strings):
        raise ValueError("Mutating SQL statements are not allowed")
