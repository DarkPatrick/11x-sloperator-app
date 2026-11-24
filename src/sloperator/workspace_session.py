"""Nested claude-workspace-ug session of one Jira code task.

The code-task orchestrator (a Claude turn in ug-ai-analyst) calls this tool through Bash::

    python -m sloperator.workspace_session send --task UMN-14 --message-file /tmp/msg.md
    python -m sloperator.workspace_session wait --task UMN-14 [--max-seconds 540]

``send`` starts one nested Claude turn in the background and returns at once; ``wait`` blocks
until that turn ends or the time limit passes (one call fits the Bash tool limit). There is one
nested session per Jira issue; its id, process and result live in ``CODE_TASK_STATE_DIR``.

The tool runs as a child of the orchestrator, which inherits the service environment without
``SLOPERATOR_*``; it therefore reads only ``CODE_TASK_*`` variables and never builds ``Settings``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

from sloperator import workspace_lock
from sloperator.claude_budget import transcript_directory
from sloperator.config import (
    DEFAULT_CODE_TASK_CLAUDE_CLI,
    DEFAULT_CODE_TASK_MODEL,
    DEFAULT_CODE_TASK_NESTED_TIMEOUT_SECONDS,
    DEFAULT_CODE_TASK_STATE_DIR,
    DEFAULT_CODE_TASK_WORKSPACE,
)

TASK_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
DEFAULT_WAIT_SECONDS = 540
MAX_WAIT_SECONDS = 570
POLL_SECONDS = 0.5
# A runner whose pid was not recorded yet is still being started by ``send``.
RUNNER_START_GRACE_SECONDS = 30
STDERR_TAIL_CHARS = 2_000
DISALLOWED_TOOLS = (
    "Bash(gh pr merge:*)",
    "Bash(gh pr ready:*)",
    "Bash(git push --force:*)",
    "Bash(git push -f:*)",
    "Bash(git push --force-with-lease:*)",
)
# Service variable -> variables of the nested session (identity of commits, pushes and PRs).
IDENTITY_VARIABLES = (
    ("CODE_TASK_GH_CONFIG_DIR", ("GH_CONFIG_DIR",)),
    ("CODE_TASK_GIT_SSH_COMMAND", ("GIT_SSH_COMMAND",)),
    ("CODE_TASK_GIT_NAME", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
    ("CODE_TASK_GIT_EMAIL", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")),
)
# Claude Code marks its own child processes; a nested CLI that sees them refuses to start.
PARENT_SESSION_VARIABLES = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
PREAMBLE = """\
AUTOMATED REVIEWER REQUEST for Jira issue {key}.
You are driven by an automated reviewer (the ug-ai-analyst code-task orchestrator), not by a
person at the keyboard: nobody can answer a question during this turn, so finish it with a clear
report. This message is the explicit request to commit, push and open pull requests whenever it
asks for them. Rules that override anything else in this message:
- Open every pull request as a draft (`gh pr create --draft`), following this workspace's PR
  rules (Jira key in the title, no Jira link in the body, no AI attribution, stack big PRs).
- Never merge a pull request, never mark one ready for review, never push to a default branch,
  never force-push a branch you did not create for this issue.
- Do not read or write Jira; the reviewer owns all Jira communication.
- No stands, browsers, VPN or Jenkins: run only checks that work headless on this server (for
  example `make be-unit`), and say which checks you could not run.
- End your reply with: worktree path(s) and branch(es), base commit, changed files, every check
  you ran with its exact command and exit code, the checks you skipped and why, and the URL of
  every pull request you opened or updated.

--- REQUEST ---
"""


class UsageError(Exception):
    """Bad input: one line on stderr, non-zero exit, nothing started."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


# --------------------------------------------------------------------------------------------
# Configuration from the inherited environment
# --------------------------------------------------------------------------------------------


def _env_path(name: str, default: str | Path) -> Path:
    return Path(os.environ.get(name, "").strip() or str(default)).expanduser()


def state_directory() -> Path:
    return _env_path("CODE_TASK_STATE_DIR", DEFAULT_CODE_TASK_STATE_DIR)


def code_workspace() -> Path:
    return _env_path("CODE_TASK_WORKSPACE", DEFAULT_CODE_TASK_WORKSPACE)


def nested_timeout_seconds() -> int:
    """Any positive integer here; the service checks the 300..86400 range at start-up."""
    raw = os.environ.get("CODE_TASK_NESTED_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_CODE_TASK_NESTED_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise UsageError("CODE_TASK_NESTED_TIMEOUT_SECONDS must be a positive integer")
    return value


def identity_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Nested-session variables mapped from the ``CODE_TASK_*`` identity settings that are set."""
    source = os.environ if environ is None else environ
    mapped: dict[str, str] = {}
    for setting, targets in IDENTITY_VARIABLES:
        value = source.get(setting, "").strip()
        if value:
            mapped.update(dict.fromkeys(targets, value))
    return mapped


def nested_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key not in PARENT_SESSION_VARIABLES
    }
    environment.update(identity_environment())
    return environment


def nested_command(session_id: str, *, resume: bool) -> list[str]:
    cli = _env_path("CODE_TASK_CLAUDE_CLI", DEFAULT_CODE_TASK_CLAUDE_CLI)
    model = os.environ.get("CODE_TASK_MODEL", "").strip() or DEFAULT_CODE_TASK_MODEL
    command = [
        str(cli), "-p", "--model", model, "--permission-mode", "auto",
        "--output-format", "json", "--disallowedTools", *DISALLOWED_TOOLS,
    ]
    command += ["--resume", session_id] if resume else ["--session-id", session_id]
    return command


def preamble(task_key: str) -> str:
    return PREAMBLE.format(key=task_key)


# --------------------------------------------------------------------------------------------
# Per-task state files
# --------------------------------------------------------------------------------------------


def _state_path(task_key: str) -> Path:
    return state_directory() / f"{task_key}.json"


def _message_path(task_key: str) -> Path:
    return state_directory() / f"{task_key}.message"


@contextlib.contextmanager
def _task_lock(task_key: str) -> Iterator[None]:
    directory = state_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"{task_key}.lock", "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_state(task_key: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(_state_path(task_key).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise UsageError(f"cannot read the nested session state of {task_key}: {error}") from error
    return payload if isinstance(payload, dict) else None


def _write_state(task_key: str, state: dict[str, Any]) -> None:
    path = _state_path(task_key)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _runner_alive(state: dict[str, Any]) -> bool:
    pid = state.get("runner_pid")
    if isinstance(pid, int) and pid > 0:
        return _alive(pid)
    try:
        started = dt.datetime.fromisoformat(str(state.get("started_at")))
    except ValueError:
        return False
    return (dt.datetime.now(dt.UTC) - started).total_seconds() < RUNNER_START_GRACE_SECONDS


def _validated_key(task_key: str) -> str:
    if not TASK_KEY_RE.fullmatch(task_key):
        raise UsageError(f"invalid Jira issue key {task_key!r}; expected for example UMN-14")
    return task_key


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


def send(task_key: str, message_file: Path) -> dict[str, Any]:
    task_key = _validated_key(task_key)
    try:
        body = message_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise UsageError(f"cannot read the message file {message_file}: {error}") from error
    if not body.strip():
        raise UsageError(f"the message file {message_file} is empty")
    workspace = code_workspace()
    if not workspace.is_dir():
        raise UsageError(f"the code workspace {workspace} does not exist")
    nested_timeout_seconds()
    with _task_lock(task_key):
        previous = read_state(task_key)
        if previous and previous.get("status") == "running" and _runner_alive(previous):
            raise UsageError(
                f"a nested turn for {task_key} is still running; wait for it before sending"
            )
        session_id = str((previous or {}).get("session_id") or uuid.uuid4())
        _message_path(task_key).write_text(preamble(task_key) + body, encoding="utf-8")
        state: dict[str, Any] = {
            "task": task_key,
            "session_id": session_id,
            "status": "running",
            "runner_pid": None,
            "started_at": _now(),
            "turns": int((previous or {}).get("turns") or 0) + 1,
        }
        _write_state(task_key, state)
        runner = subprocess.Popen(
            [sys.executable, "-m", "sloperator.workspace_session", "_run", "--task", task_key],
            cwd=state_directory(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        state["runner_pid"] = runner.pid
        _write_state(task_key, state)
    return {"status": "started", "session_id": session_id}


def _outcome(return_code: int, stdout: str, stderr: str, session_id: str) -> dict[str, Any]:
    if return_code != 0:
        tail = (stderr.strip() or stdout.strip())[-STDERR_TAIL_CHARS:]
        return {"status": "failed", "reason": f"exit code {return_code}: {tail}"}
    try:
        payload = json.loads(stdout)
    except ValueError:
        return {"status": "failed", "reason": f"invalid JSON output: {stdout.strip()[:500]}"}
    if not isinstance(payload, dict):
        return {"status": "failed", "reason": "invalid JSON output: not an object"}
    if payload.get("is_error"):
        return {"status": "failed", "reason": f"is_error: {str(payload.get('result', ''))[:2000]}"}
    return {
        "status": "done",
        "result": str(payload.get("result", "")),
        "session_id": str(payload.get("session_id") or session_id),
    }


def run_turn(task_key: str) -> None:
    """Background runner started by ``send``: one nested turn, its result into the state file."""
    task_key = _validated_key(task_key)
    state = read_state(task_key) or {}
    session_id = str(state.get("session_id") or "")
    workspace = code_workspace()
    try:
        message = _message_path(task_key).read_text(encoding="utf-8")
        timeout = nested_timeout_seconds()
        resume = (transcript_directory(workspace) / f"{session_id}.jsonl").is_file()
        command = workspace_lock.with_workspace_lock(
            workspace, nested_command(session_id, resume=resume)
        )
        process = subprocess.Popen(
            command, cwd=workspace, env=nested_environment(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
    except (OSError, UsageError) as error:
        outcome: dict[str, Any] = {"status": "failed", "reason": f"not started: {error}"}
    else:
        try:
            stdout, stderr = process.communicate(message, timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.kill()
            process.communicate()
            outcome = {"status": "failed", "reason": f"timeout after {timeout} seconds"}
        else:
            outcome = _outcome(process.returncode, stdout, stderr, session_id)
    with _task_lock(task_key):
        current = read_state(task_key) or state
        current.update(outcome)
        current["session_id"] = session_id or current.get("session_id")
        current["finished_at"] = _now()
        _write_state(task_key, current)


def wait(task_key: str, max_seconds: int) -> dict[str, Any]:
    task_key = _validated_key(task_key)
    if max_seconds < 1:
        raise UsageError("--max-seconds must be at least 1")
    max_seconds = min(max_seconds, MAX_WAIT_SECONDS)
    if read_state(task_key) is None:
        raise UsageError(f"no nested turn was ever started for {task_key}; use send first")
    deadline = time.monotonic() + max_seconds
    while True:
        state = read_state(task_key) or {}
        status = state.get("status")
        session_id = state.get("session_id")
        if status == "done":
            return {"status": "done", "result": state.get("result", ""), "session_id": session_id}
        if status in {"failed", "interrupted"}:
            return {"status": status, "reason": state.get("reason", ""), "session_id": session_id}
        if not _runner_alive(state):
            with _task_lock(task_key):
                current = read_state(task_key) or {}
                if current.get("status") == "running" and not _runner_alive(current):
                    current.update(
                        status="interrupted",
                        reason="the nested turn ended without a result (process gone)",
                        finished_at=_now(),
                    )
                    _write_state(task_key, current)
            continue
        if time.monotonic() >= deadline:
            return {"status": "running", "session_id": session_id}
        time.sleep(POLL_SECONDS)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m sloperator.workspace_session", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    send_parser = commands.add_parser("send", help="start a nested turn in the background")
    send_parser.add_argument("--task", required=True)
    send_parser.add_argument("--message-file", required=True, type=Path)
    wait_parser = commands.add_parser("wait", help="wait for the nested turn of a task")
    wait_parser.add_argument("--task", required=True)
    wait_parser.add_argument("--max-seconds", type=int, default=DEFAULT_WAIT_SECONDS)
    run_parser = commands.add_parser("_run")
    run_parser.add_argument("--task", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "send":
            print(json.dumps(send(args.task, args.message_file)))
        elif args.command == "wait":
            print(json.dumps(wait(args.task, args.max_seconds), ensure_ascii=False))
        else:
            run_turn(args.task)
    except UsageError as error:
        print(f"workspace_session: {error}".replace("\n", " "), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
