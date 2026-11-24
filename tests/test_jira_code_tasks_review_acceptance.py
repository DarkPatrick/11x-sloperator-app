"""Acceptance tests for the orchestrator review-loop prompt rules (case C9).

Written by the independent test author from the specification, before the implementation.
The four orchestrator prompts (first turn, continue, reply and the admin template) are built
offline from a ``Settings`` loaded from a clean temporary environment; nothing touches Jira,
Slack, GitHub or a Claude session.

Sensitivity replays use the runner of ``test_workspace_session_review_acceptance.py``::

    .venv/bin/python tests/test_jira_code_tasks_review_acceptance.py \
        --mutation pr-comment-heading --select test_c9_
"""

from __future__ import annotations

import dataclasses
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

VERDICTS = ("CODE_TASK_PR_OPENED", "CODE_TASK_UPDATED", "CODE_TASK_WAITING", "CODE_TASK_IDLE")
HEADINGS = ("What was run", "Results", "Fixed", "Left as is", "Checks after fixes")
REASONS = ("hallucination", "not ours", "product decision", "needs a human")
KEY = "UMN-77"


def build_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """A complete service ``Settings`` from a clean environment (no dotenv, no real secrets)."""
    from sloperator.config import Settings

    monkeypatch.setattr("sloperator.config.load_dotenv", lambda *_a, **_k: False)
    for name in list(os.environ):
        if name.startswith(("CODE_TASK_", "SLOPERATOR_", "JIRA_", "SLACK_")):
            monkeypatch.delenv(name, raising=False)
    environment = {
        "SLACK_USER_ID": "UOWNER",
        "SLACK_ALLOWED_CONVERSATION_USERS": "UALICE",
        "SLOPERATOR_SLACK_BOT_TOKEN": "xoxb-test",
        "SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID": "xapp-test",
        "SLOPERATOR_DATABASE_PATH": str(tmp_path / "db.sqlite3"),
        "SLOPERATOR_AGENT_WORKSPACE": str(tmp_path / "agent-workspace"),
        "SLOPERATOR_CLAUDE_CLI": str(tmp_path / "no-real-claude"),
        "JIRA_USERNAME": "service@example.com",
        "JIRA_API_TOKEN": "test-token",
        "CODE_TASK_ENABLED": "true",
        "CODE_TASK_WORKSPACE": str(tmp_path / "claude-workspace-ug"),
        "CODE_TASK_STATE_DIR": str(tmp_path / "code-task-state"),
        "CODE_TASK_CLAUDE_CLI": str(tmp_path / "no-real-claude"),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return dataclasses.replace(Settings.from_environment(), jira_url="https://jira.example.com")


def prompts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, tuple[str, str]]:
    """Prompt name -> (text, the task key the text uses)."""
    from sloperator import jira_code_tasks as tasks

    settings = build_settings(monkeypatch, tmp_path)
    return {
        "first_turn": (tasks.first_turn_prompt(settings, KEY), KEY),
        "continue": (tasks.continue_prompt(KEY), KEY),
        "reply": (tasks.reply_prompt(KEY, "4242"), KEY),
        "admin_template": (tasks.ORCHESTRATOR_PROMPT, "<KEY>"),
    }


def normalized(text: str) -> str:
    return " ".join(text.split())


PROMPT_NAMES = ["first_turn", "continue", "reply", "admin_template"]


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_c9_prompt_runs_the_review_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    raw, key = prompts(monkeypatch, tmp_path)[name]
    text = normalized(raw)
    lowered = text.casefold()

    assert f"-m sloperator.workspace_session review --task {key} --dir" in text, text
    assert "--kind review" in text
    assert "/deep-review-local" in text
    assert re.search(
        r"\b(3|three)\b.{0,60}(review rounds?|rounds? per (review )?pass)"
        r"|(review rounds?|rounds? per (review )?pass).{0,60}\b(3|three)\b",
        lowered,
    ), "the limit of 3 review rounds is missing"
    assert "review round limit reached" in lowered
    for word in (r"\breal\b", r"\bours\b", r"\bfixable\b"):
        assert re.search(word, lowered), f"{word} is missing from the forwarding rule"
    assert re.search(r"\bre-?run\b", lowered), "the rule to re-run the checks is missing"
    for reason in REASONS:
        assert reason in lowered, f"the reason {reason!r} is missing"
    assert "Нечего ревьюить" in text, "the nothing-to-review report is not explained"


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_c9_prompt_orders_the_review_before_the_pr_and_after_corrections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    raw, _key = prompts(monkeypatch, tmp_path)[name]
    lowered = normalized(raw).casefold()

    assert re.search(
        r"before (the |any |a )?(draft )?(pr|pull request)s? (is |are )?(opened|open)"
        r"|before open(ing)? (the |any |a )?(draft )?(pr|pull request)",
        lowered,
    ), "the review is not placed before opening the PR"
    assert re.search(r"after (every|each)\b[^.]{0,40}correction", lowered), (
        "the review is not repeated after every pushed correction"
    )


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_c9_prompt_posts_a_separate_pr_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    raw, _key = prompts(monkeypatch, tmp_path)[name]
    text = normalized(raw)
    lowered = text.casefold()

    assert "gh pr comment" in text
    assert "--body-file" in text
    assert "nested session" in lowered
    assert re.search(r"(never|not)[^.]{0,80}pr description", lowered), (
        "the rule to keep the status out of the PR description is missing"
    )
    assert re.search(r"\bnew\b[^.]{0,40}comment", lowered), "no new comment per correction pass"
    assert re.search(r"(never|not)[^.]{0,40}\bedit", lowered), "earlier comments may be edited"
    assert "Local deep review (/deep-review-local)" in text
    for heading in HEADINGS:
        assert heading in text, f"the PR comment heading {heading!r} is missing"


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_c9_prompt_reports_the_review_in_jira(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    raw, _key = prompts(monkeypatch, tmp_path)[name]
    text = normalized(raw)
    lowered = text.casefold()

    assert "verdict" in lowered
    assert re.search(
        r"(link|url)[^.]{0,60}(pr|review) comment|(pr|review) comment[^.]{0,60}(link|url)",
        lowered,
    ), "the Jira line with the link to the PR comment is missing"
    assert "/deep-review-local is not available for" in text
    for verdict in VERDICTS:
        assert verdict in text, verdict
    assert "PR_URL:" in text


if __name__ == "__main__":
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "code_task_review_acceptance_runner",
        Path(__file__).with_name("test_workspace_session_review_acceptance.py"),
    )
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    sys.exit(runner.sensitivity_main(sys.argv[1:], Path(__file__)))
