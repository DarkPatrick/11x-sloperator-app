from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sloperator import agents
from sloperator.agent_usage import AgentUsageContext, TokenUsage
from sloperator.claude_profiles import claude_profile_options, profile_directory
from sloperator.config import Settings

FAKE_BUILDER = """
import json, sys
from pathlib import Path
name, out = sys.argv[2], Path(sys.argv[4])
if name == "broken":
    sys.exit("unknown section")
out.mkdir(parents=True, exist_ok=True)
(out / f"{name}.md").write_text("# profile")
settings = {"claudeMdExcludes": ["/w/CLAUDE.md"], "skillOverrides": {"x": "off"}}
print(json.dumps({"append_system_prompt_file": str(out / f"{name}.md"), "settings": settings}))
"""


def make_settings(tmp_path: Path, workflows: set[str]) -> Settings:
    workspace = tmp_path / "workspace"
    (workspace / ".claude" / "profiles").mkdir(parents=True)
    (workspace / ".claude" / "profiles" / "build_profile.py").write_text(FAKE_BUILDER)
    python = workspace / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    return Settings(
        slack_user_id="U1234567890",
        bot_token="xoxb-test",
        app_token="xapp-test",
        database_path=tmp_path / "data" / "events.sqlite3",
        agent_workspace=workspace,
        claude_profile_workflows=frozenset(workflows),
    )


def test_profile_options_for_enabled_workflow(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, {"experiment-design"})

    options = claude_profile_options(settings, "experiment-design")

    assert options[0] == "--settings"
    assert json.loads(options[1]) == {
        "claudeMdExcludes": ["/w/CLAUDE.md"],
        "skillOverrides": {"x": "off"},
    }
    assert options[2:] == (
        "--append-system-prompt-file",
        str(profile_directory(settings) / "experiment-design.md"),
    )


def test_other_workflows_keep_full_context(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, {"experiment-design"})

    assert claude_profile_options(settings, "mobile-health") == ()


def test_builder_failure_falls_back_to_full_context(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_settings(tmp_path, {"broken"})

    assert claude_profile_options(settings, "broken") == ()
    assert "running with full context" in caplog.text


def test_missing_builder_falls_back_to_full_context(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, {"experiment-design"})
    (settings.agent_workspace / ".claude" / "profiles" / "build_profile.py").unlink()

    assert claude_profile_options(settings, "experiment-design") == ()


async def test_run_claude_appends_profile_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, {"experiment-finalizer"})
    seen: dict[str, object] = {}

    async def fake_run(*_args: object, **kwargs: object) -> agents.AgentRunResult:
        seen.update(kwargs)
        return agents.AgentRunResult(session_id="s", text="ok")

    async def fake_record(_settings, _session, _context, operation, **_kwargs):  # type: ignore[no-untyped-def]
        return await operation()

    monkeypatch.setattr(agents, "_run_claude", fake_run)
    monkeypatch.setattr(agents, "_recorded_agent_call", fake_record)
    monkeypatch.setattr(agents, "transcript_usage", lambda *_: TokenUsage())
    session = AsyncMock()
    session.external_session_id = "s"
    context = AgentUsageContext(
        agent_name="experiment-finalizer/worker",
        workflow="experiment-finalizer",
        role="worker",
        source="scheduled",
    )

    await agents.run_claude(
        settings,
        session,
        "prompt",
        AsyncMock(),
        command_options=("--tools", ""),
        usage_context=context,
    )

    options = seen["command_options"]
    assert isinstance(options, tuple)
    assert options[:2] == ("--tools", "")
    assert options[2] == "--settings"
    assert options[4] == "--append-system-prompt-file"
