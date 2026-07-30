from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sloperator.admin_sql import (
    SQL_INITIAL_INSTRUCTION,
    VIZ_INSTRUCTION,
    AdminSqlManager,
    _strip_markdown_fence,
    _validate_read_only_sql,
)
from sloperator.agents import AgentRunResult
from sloperator.config import Settings
from sloperator.store import EventStore


def test_sql_agent_prompt_is_sql_only_and_loads_ug_context() -> None:
    assert "context/rules/sql-style.md" in SQL_INITIAL_INSTRUCTION
    assert "context/ultimate-guitar-product-context.md" in SQL_INITIAL_INSTRUCTION
    assert "clickhouse-worker" in SQL_INITIAL_INSTRUCTION
    assert "ug-experiment-calculator" in SQL_INITIAL_INSTRUCTION
    assert "Do not execute queries" in SQL_INITIAL_INSTRUCTION
    assert "Do not use Markdown fences" in SQL_INITIAL_INSTRUCTION


def test_strip_markdown_fence_handles_disobedient_provider() -> None:
    assert _strip_markdown_fence("```sql\nSELECT 1;\n```") == "SELECT 1;"
    assert _strip_markdown_fence("SELECT 1;") == "SELECT 1;"


def test_sql_execution_accepts_queries_and_rejects_mutations() -> None:
    _validate_read_only_sql("-- context\nWITH 1 AS value SELECT value;")
    _validate_read_only_sql("SELECT 'drop table is text' AS harmless")
    with pytest.raises(ValueError, match="read-only"):
        _validate_read_only_sql("DROP TABLE important")
    with pytest.raises(ValueError, match="Mutating"):
        _validate_read_only_sql("WITH 1 AS value INSERT INTO target SELECT value")
    with pytest.raises(ValueError, match="one SQL"):
        _validate_read_only_sql("SELECT 1; SELECT 2")


def test_visualization_prompt_uses_sample_and_runtime_data_placeholder() -> None:
    assert "context/dataviz/principles.md" in VIZ_INSTRUCTION
    assert "const payload = __SLOPERATOR_DATA__;" in VIZ_INSTRUCTION
    assert "top 20 sample rows" not in VIZ_INSTRUCTION
    assert "do not use UG fonts" in VIZ_INSTRUCTION
    assert "at most three" in VIZ_INSTRUCTION


async def test_sql_manager_resumes_provider_session(tmp_path: Path) -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        agent_workspace=tmp_path,
    )
    store = EventStore(tmp_path / "archive.sqlite3")
    store.initialize()
    manager = AdminSqlManager(settings, store)
    runner = AsyncMock(
        side_effect=[
            AgentRunResult("provider-session", "```sql\nSELECT 1;\n```"),
            AgentRunResult("provider-session", "SELECT 2;"),
        ]
    )

    with patch("sloperator.admin_sql.run_claude", runner):
        assert await manager.complete("browser-session", "claude", "SELECT") == "SELECT 1;"
        assert await manager.complete("browser-session", "claude", "SELECT 2") == "SELECT 2;"

    first_session = runner.call_args_list[0].args[1]
    second_session = runner.call_args_list[1].args[1]
    assert first_session.external_session_id is None
    assert second_session.external_session_id == "provider-session"
    assert second_session.turn_count == 1
    assert runner.call_args_list[0].kwargs["command_options"] == ("--safe-mode",)
    assert runner.call_args_list[0].kwargs["environment_overrides"] == {
        "UG_SKIP_PREFLIGHT": "1"
    }
