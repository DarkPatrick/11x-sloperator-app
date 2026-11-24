"""Unit tests for helpers of sloperator.workspace_session (acceptance tests live separately)."""

from __future__ import annotations

from pathlib import Path

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


def test_review_prompt_takes_one_line_of_focus() -> None:
    assert session.review_prompt(None) == "/deep-review-local"
    assert session.review_prompt("  ") == "/deep-review-local"
    assert session.review_prompt(" sql ") == "/deep-review-local sql"
    for bad in ("a\nb", "a\rb", "a" + chr(0x2028) + "b", "trailing\n"):
        with pytest.raises(session.UsageError):
            session.review_prompt(bad)


def test_review_command_is_fresh_and_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_TASK_REVIEW_MODEL", " ")
    monkeypatch.setenv("CODE_TASK_MODEL", "m2")
    command = session.review_command("sid")
    assert command[-2:] == ["--session-id", "sid"]
    assert "--resume" not in command
    assert command[command.index("--model") + 1] == "m2"
    for entry in ("Edit", "Write", "NotebookEdit", "Bash(git commit:*)", "Bash(git push:*)",
                  "Bash(gh pr merge:*)"):
        assert entry in command


def test_review_directory_accepts_the_root_children_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, base = tmp_path / "ws", tmp_path / "wt"
    checkout = base / "UMN-1" / "backend"
    (checkout / ".claude" / "commands").mkdir(parents=True)
    (checkout / ".claude" / "commands" / "deep-review-local.md").write_text("x")
    workspace.mkdir()
    monkeypatch.setenv("CODE_TASK_WORKSPACE", str(workspace))
    monkeypatch.setenv("CODE_TASK_WORKTREE_BASE", str(base))
    assert session.review_directory(checkout) == checkout.resolve()
    monkeypatch.setenv("CODE_TASK_WORKTREE_BASE", str(tmp_path / "w"))
    with pytest.raises(session.UsageError, match="outside"):
        session.review_directory(checkout)
