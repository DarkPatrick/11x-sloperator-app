"""Nested claude-workspace-ug session of one Jira code task.

The code-task orchestrator (a Claude turn in ug-ai-analyst) calls this tool through Bash::

    python -m sloperator.workspace_session send --task UMN-14 --message-file /tmp/msg.md
    python -m sloperator.workspace_session wait --task UMN-14 [--max-seconds 540]
    python -m sloperator.workspace_session review --task UMN-14 --dir <worktree> [--focus TEXT]
    python -m sloperator.workspace_session wait --task UMN-14 --kind review [--max-seconds 540]

``send`` starts one nested Claude turn in the background and returns at once; ``wait`` blocks
until that turn ends or the time limit passes (one call fits the Bash tool limit). There is one
nested session per Jira issue; its id, process and result live in ``CODE_TASK_STATE_DIR``.

``review`` starts one headless, read-only ``/deep-review-local`` run (a fresh Claude session
each time) in a product checkout under ``CODE_TASK_WORKSPACE`` or ``CODE_TASK_WORKTREE_BASE``;
its state lives in ``<KEY>.review.json`` and never touches the nested session's ``<KEY>.json``.
A task never runs a review and a nested turn at the same time.

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
    DEFAULT_CODE_TASK_WORKTREE_BASE,
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
# A review run only reads: no file edits, no commits, no pushes.
REVIEW_DISALLOWED_TOOLS = (
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash(git commit:*)",
    "Bash(git push:*)",
)
REVIEW_COMMAND = "/deep-review-local"
REVIEW_COMMAND_FILE = Path(".claude") / "commands" / "deep-review-local.md"
NESTED = "nested"
REVIEW = "review"
KINDS = (NESTED, REVIEW)
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
- Work only in product worktrees created with this workspace's
  `make task-start KEY={key} REPOS=<repos>` or `make wt-init` (they live under the workspace's
  `WT_BASE`). Never check out, create or switch a branch in the main clones inside this
  workspace; leave them on their default branch.
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


def worktree_base() -> Path:
    return _env_path("CODE_TASK_WORKTREE_BASE", DEFAULT_CODE_TASK_WORKTREE_BASE)


def review_model() -> str:
    for name in ("CODE_TASK_REVIEW_MODEL", "CODE_TASK_MODEL"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return DEFAULT_CODE_TASK_MODEL


def nested_command(session_id: str, *, resume: bool) -> list[str]:
    cli = _env_path("CODE_TASK_CLAUDE_CLI", DEFAULT_CODE_TASK_CLAUDE_CLI)
    model = os.environ.get("CODE_TASK_MODEL", "").strip() or DEFAULT_CODE_TASK_MODEL
    command = [
        str(cli), "-p", "--model", model, "--permission-mode", "auto",
        "--output-format", "json", "--disallowedTools", *DISALLOWED_TOOLS,
    ]
    command += ["--resume", session_id] if resume else ["--session-id", session_id]
    return command


def review_command(session_id: str) -> list[str]:
    """A fresh read-only session; the prompt goes on stdin, so the slash command comes first."""
    cli = _env_path("CODE_TASK_CLAUDE_CLI", DEFAULT_CODE_TASK_CLAUDE_CLI)
    return [
        str(cli), "-p", "--model", review_model(), "--permission-mode", "auto",
        "--output-format", "json", "--disallowedTools", *DISALLOWED_TOOLS,
        *REVIEW_DISALLOWED_TOOLS, "--session-id", session_id,
    ]


def review_prompt(focus: str | None) -> str:
    if focus is None:
        return REVIEW_COMMAND
    if focus and focus.splitlines() != [focus]:
        raise UsageError("--focus must be one line of text without line breaks")
    focus = focus.strip()
    return f"{REVIEW_COMMAND} {focus}" if focus else REVIEW_COMMAND


def review_directory(directory: Path | str) -> Path:
    """The resolved checkout, inside one of the two roots and with the review command file."""
    try:
        resolved = Path(directory).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise UsageError(f"--dir {directory} does not exist") from error
    if not resolved.is_dir():
        raise UsageError(f"--dir {directory} is not a directory")
    roots = (code_workspace().resolve(), worktree_base().resolve())
    if not any(resolved.is_relative_to(root) for root in roots):
        raise UsageError(
            f"--dir {resolved} is outside the code workspace {roots[0]} and the worktree base "
            f"{roots[1]}"
        )
    if not (resolved / REVIEW_COMMAND_FILE).is_file():
        raise UsageError(
            f"{REVIEW_COMMAND} is not available in {resolved}: it has no {REVIEW_COMMAND_FILE}"
        )
    return resolved


def preamble(task_key: str) -> str:
    return PREAMBLE.format(key=task_key)


# --------------------------------------------------------------------------------------------
# Per-task state files
# --------------------------------------------------------------------------------------------


def _state_path(task_key: str, kind: str = NESTED) -> Path:
    name = f"{task_key}.review.json" if kind == REVIEW else f"{task_key}.json"
    return state_directory() / name


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


def read_state(task_key: str, kind: str = NESTED) -> dict[str, Any] | None:
    try:
        payload = json.loads(_state_path(task_key, kind).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise UsageError(f"cannot read the {kind} state of {task_key}: {error}") from error
    return payload if isinstance(payload, dict) else None


def _write_state(task_key: str, state: dict[str, Any], kind: str = NESTED) -> None:
    path = _state_path(task_key, kind)
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


def _running(state: dict[str, Any] | None) -> bool:
    return state is not None and state.get("status") == "running" and _runner_alive(state)


def _start_runner(task_key: str, kind: str) -> int:
    runner = subprocess.Popen(
        [sys.executable, "-m", "sloperator.workspace_session", "_run", "--task", task_key,
         "--kind", kind],
        cwd=state_directory(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    return runner.pid


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
        if _running(previous):
            raise UsageError(
                f"a nested turn for {task_key} is still running; wait for it before sending"
            )
        if _running(read_state(task_key, REVIEW)):
            raise UsageError(
                f"a review of {task_key} is still running; wait --kind review before sending"
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
        state["runner_pid"] = _start_runner(task_key, NESTED)
        _write_state(task_key, state)
    return {"status": "started", "session_id": session_id}


def review(task_key: str, directory: Path | str, focus: str | None) -> dict[str, Any]:
    """Start one fresh ``/deep-review-local`` run in a checkout; the nested state is only read."""
    task_key = _validated_key(task_key)
    prompt = review_prompt(focus)
    checkout = review_directory(directory)
    nested_timeout_seconds()
    with _task_lock(task_key):
        previous = read_state(task_key, REVIEW)
        if _running(previous):
            raise UsageError(
                f"a review of {task_key} is still running; wait --kind review before a new one"
            )
        if _running(read_state(task_key)):
            raise UsageError(
                f"a nested turn for {task_key} is still running; wait for it before a review"
            )
        session_id = str(uuid.uuid4())
        state: dict[str, Any] = {
            "task": task_key,
            "kind": REVIEW,
            "session_id": session_id,
            "status": "running",
            "runner_pid": None,
            "started_at": _now(),
            "directory": str(checkout),
            "prompt": prompt,
            "runs": int((previous or {}).get("runs") or 0) + 1,
        }
        _write_state(task_key, state, REVIEW)
        state["runner_pid"] = _start_runner(task_key, REVIEW)
        _write_state(task_key, state, REVIEW)
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


def _execute(command: list[str], cwd: Path, stdin_text: str, timeout: int,
             session_id: str) -> dict[str, Any]:
    """Run one Claude CLI process in its own process group; killed after ``timeout`` seconds."""
    process = subprocess.Popen(
        command, cwd=cwd, env=nested_environment(), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        process.communicate()
        return {"status": "failed", "reason": f"timeout after {timeout} seconds"}
    return _outcome(process.returncode, stdout, stderr, session_id)


def _finish(task_key: str, kind: str, state: dict[str, Any], outcome: dict[str, Any],
            session_id: str) -> None:
    with _task_lock(task_key):
        current = read_state(task_key, kind) or state
        current.update(outcome)
        current["session_id"] = session_id or current.get("session_id")
        current["finished_at"] = _now()
        _write_state(task_key, current, kind)


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
        outcome = _execute(command, workspace, message, timeout, session_id)
    except (OSError, UsageError) as error:
        outcome = {"status": "failed", "reason": f"not started: {error}"}
    _finish(task_key, NESTED, state, outcome, session_id)


def run_review(task_key: str) -> None:
    """Background runner started by ``review``: one review run, its report into its state."""
    task_key = _validated_key(task_key)
    state = read_state(task_key, REVIEW) or {}
    session_id = str(state.get("session_id") or "")
    try:
        timeout = nested_timeout_seconds()
        directory = Path(str(state.get("directory") or ""))
        if not session_id or not directory.is_absolute():
            raise UsageError(f"the review state of {task_key} is incomplete")
        prompt = str(state.get("prompt") or REVIEW_COMMAND)
        outcome = _execute(review_command(session_id), directory, prompt, timeout, session_id)
    except (OSError, UsageError) as error:
        outcome = {"status": "failed", "reason": f"not started: {error}"}
    _finish(task_key, REVIEW, state, outcome, session_id)


def wait(task_key: str, max_seconds: int, kind: str = NESTED) -> dict[str, Any]:
    task_key = _validated_key(task_key)
    if kind not in KINDS:
        raise UsageError(f"--kind must be one of {', '.join(KINDS)}")
    if max_seconds < 1:
        raise UsageError("--max-seconds must be at least 1")
    max_seconds = min(max_seconds, MAX_WAIT_SECONDS)
    if read_state(task_key, kind) is None:
        if kind == REVIEW:
            raise UsageError(f"no review was ever started for {task_key}; use review first")
        raise UsageError(f"no nested turn was ever started for {task_key}; use send first")
    what = "review run" if kind == REVIEW else "nested turn"
    deadline = time.monotonic() + max_seconds
    while True:
        state = read_state(task_key, kind) or {}
        status = state.get("status")
        session_id = state.get("session_id")
        if status == "done":
            return {"status": "done", "result": state.get("result", ""), "session_id": session_id}
        if status in {"failed", "interrupted"}:
            return {"status": status, "reason": state.get("reason", ""), "session_id": session_id}
        if not _runner_alive(state):
            with _task_lock(task_key):
                current = read_state(task_key, kind) or {}
                if current.get("status") == "running" and not _runner_alive(current):
                    current.update(
                        status="interrupted",
                        reason=f"the {what} ended without a result (process gone)",
                        finished_at=_now(),
                    )
                    _write_state(task_key, current, kind)
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
    wait_parser.add_argument("--kind", choices=KINDS, default=NESTED,
                             help="nested (the default): the nested turn; review: the review run")
    review_parser = commands.add_parser(
        "review", help="start a read-only /deep-review-local run in a checkout in the background"
    )
    review_parser.add_argument("--task", required=True)
    review_parser.add_argument("--dir", required=True, help="product checkout to review")
    review_parser.add_argument("--focus", help="optional one-line focus text for the review")
    run_parser = commands.add_parser("_run")
    run_parser.add_argument("--task", required=True)
    run_parser.add_argument("--kind", choices=KINDS, default=NESTED)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "send":
            print(json.dumps(send(args.task, args.message_file)))
        elif args.command == "wait":
            print(json.dumps(wait(args.task, args.max_seconds, args.kind), ensure_ascii=False))
        elif args.command == "review":
            print(json.dumps(review(args.task, args.dir, args.focus)))
        elif args.kind == REVIEW:
            run_review(args.task)
        else:
            run_turn(args.task)
    except UsageError as error:
        print(f"workspace_session: {error}".replace("\n", " "), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
