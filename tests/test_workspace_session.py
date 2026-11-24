"""Unit tests for helpers of sloperator.workspace_session (acceptance tests live separately)."""

from __future__ import annotations

import pytest

from sloperator import workspace_session as session


def test_identity_environment_maps_only_the_set_variables() -> None:
    assert session.identity_environment({}) == {}
    assert session.identity_environment({
        "CODE_TASK_GIT_EMAIL": "bot@example.com", "CODE_TASK_GH_CONFIG_DIR": "  ",
    }) == {"GIT_AUTHOR_EMAIL": "bot@example.com", "GIT_COMMITTER_EMAIL": "bot@example.com"}


def test_nested_environment_drops_the_parent_claude_session_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    environment = session.nested_environment()
    assert "CLAUDECODE" not in environment
    assert "CLAUDE_CODE_ENTRYPOINT" not in environment


def test_nested_command_puts_the_session_option_after_the_disallowed_tools() -> None:
    command = session.nested_command("sid", resume=True)
    assert command[-2:] == ["--resume", "sid"]
    assert session.nested_command("sid", resume=False)[-2:] == ["--session-id", "sid"]
    assert "Bash(gh pr merge:*)" in command


def test_failed_outcomes_name_the_reason() -> None:
    assert session._outcome(3, "", "noise\nboom", "s")["reason"] == "exit code 3: noise\nboom"
    assert "invalid JSON" in session._outcome(0, "nope", "", "s")["reason"]
    assert session._outcome(0, '{"is_error": true}', "", "s")["reason"].startswith("is_error")
    done = session._outcome(0, '{"result": "ok"}', "", "s")
    assert done == {"status": "done", "result": "ok", "session_id": "s"}


def test_nested_timeout_accepts_any_positive_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_TASK_NESTED_TIMEOUT_SECONDS", "2")
    assert session.nested_timeout_seconds() == 2
    monkeypatch.setenv("CODE_TASK_NESTED_TIMEOUT_SECONDS", "0")
    with pytest.raises(session.UsageError):
        session.nested_timeout_seconds()
