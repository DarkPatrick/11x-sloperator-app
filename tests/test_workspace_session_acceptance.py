"""Acceptance tests for ``python -m sloperator.workspace_session`` (cases C10, C11, E5, E6).

Written by the independent test author from the specification, before the implementation.
The nested Claude CLI is a stub executable and ``flock`` is a stub that takes a real advisory
lock; the stub flock is installed in every Python process of the tool through a
``sitecustomize`` module that sets ``sloperator.workspace_lock.FLOCK``. Nothing touches the
network or a real Claude session.

Sensitivity replays use the runner of ``test_jira_code_tasks_acceptance.py``::

    .venv/bin/python tests/test_workspace_session_acceptance.py --mutation nested-resume \
        --select test_c10_
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
IDENTITY_ENV = ("GH_CONFIG_DIR", "GIT_SSH_COMMAND", "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME",
                "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")

STUB_CLAUDE = """\
import json, os, re, select, sys, time
from pathlib import Path

IDENTITY = {identity!r}
args = sys.argv[1:]
stdin_text = ""
try:
    if sys.stdin is not None and select.select([sys.stdin], [], [], 2.0)[0]:
        stdin_text = sys.stdin.read()
except (OSError, ValueError):
    pass
control_path = Path(os.environ["ACCEPTANCE_STUB_CONTROL"])
control = json.loads(control_path.read_text()) if control_path.exists() else {{}}
mode = control.get("mode", "ok")


def option(name):
    if name in args and args.index(name) + 1 < len(args):
        return args[args.index(name) + 1]
    return None


session = option("--session-id") or option("--resume") or ""
record = {{
    "event": "start", "argv": args, "cwd": os.getcwd(), "stdin": stdin_text,
    "pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgid(0),
    "env": {{key: os.environ[key] for key in IDENTITY if key in os.environ}},
}}
with open(os.environ["ACCEPTANCE_STUB_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
transcripts = config / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(Path.cwd().resolve()))
transcript = transcripts / (session + ".jsonl")
if "--resume" in args and not transcript.exists():
    print("No conversation found with session ID: " + session, file=sys.stderr)
    sys.exit(1)
if mode == "hang":
    time.sleep(float(control.get("seconds", 60)))
if mode == "crash":
    print("stub crashed before saving a transcript", file=sys.stderr)
    sys.exit(1)
if mode == "exit_nonzero":
    print("first line of noise", file=sys.stderr)
    print("boom-stderr-tail", file=sys.stderr)
    sys.exit(3)
if mode == "invalid_json":
    print("this is not json at all")
    sys.exit(0)
time.sleep(float(control.get("delay", 0)))
transcripts.mkdir(parents=True, exist_ok=True)
transcript.write_text('{{"type": "user"}}\\n', encoding="utf-8")
if mode == "is_error":
    print(json.dumps({{"type": "result", "is_error": True, "result": "API Error: overloaded",
                      "session_id": session}}))
    sys.exit(0)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": control.get("result", "stub result"), "session_id": session}}))
"""

STUB_FLOCK = """\
import fcntl, json, os, sys

args = sys.argv[1:]
index, options = 0, []
while index < len(args) and args[index].startswith("-"):
    options.append(args[index])
    if args[index] in ("-w", "--wait", "--timeout", "-E", "--conflict-exit-code"):
        index += 1
        options.append(args[index])
    index += 1
lock, command = args[index], args[index + 1:]
with open(os.environ["ACCEPTANCE_FLOCK_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"options": options, "lock": lock, "command": command}) + "\\n")
descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
fcntl.flock(descriptor, fcntl.LOCK_EX)
os.set_inheritable(descriptor, True)
os.execv(command[0], command)
"""

SITECUSTOMIZE = """\
import os

_stub = os.environ.get("ACCEPTANCE_FLOCK_STUB")
if _stub:
    try:
        import sloperator.workspace_lock as _workspace_lock

        _workspace_lock.FLOCK = _stub
    except Exception:
        pass
"""


def _executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _process_state(pid: int) -> str:
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return result.stdout.strip()


def alive(pid: int) -> bool:
    state = _process_state(pid)
    return bool(state) and not state.startswith("Z")


class NestedTool:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.workspace = tmp_path / "claude-workspace-ug"
        self.workspace.mkdir()
        self.state = tmp_path / "code-task-state"
        self.claude_config = tmp_path / "claude-config"
        self.claude_config.mkdir()
        self.stub_log = tmp_path / "stub-claude.log"
        self.flock_log = tmp_path / "stub-flock.log"
        self.control = tmp_path / "stub-control.json"
        site = tmp_path / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        self.site = site
        self.claude = _executable(tmp_path / "claude", STUB_CLAUDE.format(identity=IDENTITY_ENV))
        self.flock = _executable(tmp_path / "flock", STUB_FLOCK)
        self.overrides: dict[str, str | None] = {}
        self.set_mode("ok")

    def set_mode(self, mode: str, **values: Any) -> None:
        self.control.write_text(json.dumps({"mode": mode, **values}), encoding="utf-8")

    def environment(self) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("CODE_TASK_") and key not in IDENTITY_ENV and key != "GH_TOKEN"
        }
        environment.update({
            "PYTHONPATH": os.pathsep.join([str(self.site), str(SRC)]),
            "CODE_TASK_CLAUDE_CLI": str(self.claude),
            "CODE_TASK_WORKSPACE": str(self.workspace),
            "CODE_TASK_STATE_DIR": str(self.state),
            "CLAUDE_CONFIG_DIR": str(self.claude_config),
            "ACCEPTANCE_FLOCK_STUB": str(self.flock),
            "ACCEPTANCE_FLOCK_LOG": str(self.flock_log),
            "ACCEPTANCE_STUB_LOG": str(self.stub_log),
            "ACCEPTANCE_STUB_CONTROL": str(self.control),
        })
        for key, value in self.overrides.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        return environment

    def run(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sloperator.workspace_session", *args],
            cwd=self.tmp, env=self.environment(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout,
        )

    def message(self, text: str, name: str = "message.txt") -> Path:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def send(self, key: str, text: str) -> dict:
        result = self.run("send", "--task", key, "--message-file", str(self.message(text)))
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def wait(self, key: str, seconds: int = 40) -> dict:
        result = self.run("wait", "--task", key, "--max-seconds", str(seconds),
                          timeout=seconds + 30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def starts(self) -> list[dict]:
        if not self.stub_log.exists():
            return []
        lines = self.stub_log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def flocks(self) -> list[dict]:
        if not self.flock_log.exists():
            return []
        lines = self.flock_log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def wait_for_starts(self, count: int, limit: float = 20) -> list[dict]:
        deadline = time.monotonic() + limit
        while len(self.starts()) < count:
            assert time.monotonic() < deadline, "the stub Claude CLI was not started"
            time.sleep(0.05)
        return self.starts()

    def kill_tree(self, record: dict) -> None:
        own_group = os.getpgid(0)
        if record["pgid"] != own_group:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(record["pgid"], signal.SIGKILL)
        for pid in (record["pid"], record["ppid"]):
            if pid in (1, os.getpid()):
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)

    def cleanup(self) -> None:
        own_group = os.getpgid(0)
        for record in self.starts():
            if record["pgid"] != own_group:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(record["pgid"], signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(record["pid"], signal.SIGKILL)


@pytest.fixture
def tool(tmp_path: Path) -> Iterator[NestedTool]:
    created = NestedTool(tmp_path)
    yield created
    created.cleanup()


def message_of(record: dict, marker: str) -> str:
    """The prompt the stub received, from stdin or from the argument that carries it."""
    if marker in record["stdin"]:
        return str(record["stdin"])
    carriers = [arg for arg in record["argv"] if marker in arg]
    assert carriers, f"the message with {marker!r} reached the stub neither by stdin nor argv"
    return str(carriers[0])


def disallowed_tools(record: dict, marker: str) -> str:
    argv = record["argv"]
    values: list[str] = []
    for index, arg in enumerate(argv):
        if arg in ("--disallowedTools", "--disallowed-tools"):
            for value in argv[index + 1:]:
                if value.startswith("--") and not value.startswith("--force"):
                    break
                if marker not in value:
                    values.append(value)
        elif arg.startswith(("--disallowedTools=", "--disallowed-tools=")):
            values.append(arg.split("=", 1)[1])
    return "\n".join(values)


def option_value(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def preamble(message: str, body: str) -> str:
    assert body in message, "the message file text is missing"
    head = message[: message.index(body)]
    assert head.strip(), "the message does not start with the fixed preamble"
    return head


# --------------------------------------------------------------------------------------------
# C10 - happy path (criteria 10 and 1)
# --------------------------------------------------------------------------------------------


def test_c10_send_wait_and_resume(tool: NestedTool) -> None:
    from sloperator.workspace_lock import workspace_lock_path

    body_one = "Implement the tab flag change. MARK-C10-ONE"
    tool.set_mode("ok", delay=4, result="first nested result")
    began = time.monotonic()
    started = tool.send("UMN-14", body_one)
    elapsed = time.monotonic() - began

    assert started["status"] == "started"
    session_id = started["session_id"]
    assert isinstance(session_id, str) and session_id
    assert elapsed < 3.5, f"send waited for the nested turn ({elapsed:.1f}s)"

    done = tool.wait("UMN-14")
    assert done["status"] == "done", done
    assert done["result"] == "first nested result"
    assert done["session_id"] == session_id

    (first,) = tool.starts()
    argv = first["argv"]
    assert "-p" in argv
    assert option_value(argv, "--session-id") == session_id
    assert "--resume" not in argv
    assert option_value(argv, "--model") == "claude-opus-5-5"
    assert option_value(argv, "--permission-mode") == "auto"
    assert option_value(argv, "--output-format") == "json"
    blocked = disallowed_tools(first, "MARK-C10-ONE")
    for command in ("gh pr merge", "gh pr ready", "git push -f", "--force-with-lease"):
        assert command in blocked, f"{command!r} is not disallowed: {blocked!r}"
    assert any(
        "git push --force" in line.replace("--force-with-lease", "")
        for line in blocked.splitlines()
    ), blocked
    assert os.path.realpath(first["cwd"]) == os.path.realpath(tool.workspace)
    assert first["env"] == {}, "identity variables leaked although none is configured"
    (lock,) = tool.flocks()
    assert Path(lock["lock"]) == workspace_lock_path(tool.workspace)
    first_message = message_of(first, "MARK-C10-ONE")
    head_one = preamble(first_message, body_one)
    assert "UMN-14" in head_one
    assert "draft" in head_one.casefold()
    assert "merge" in head_one.casefold()

    body_two = "Rename the flag to tab_v2. MARK-C10-TWO"
    tool.set_mode("ok", result="second nested result")
    again = tool.send("UMN-14", body_two)
    assert again["status"] == "started"
    done = tool.wait("UMN-14")
    assert done["status"] == "done", done
    assert done["result"] == "second nested result"
    second = tool.starts()[1]
    assert option_value(second["argv"], "--resume") == session_id
    assert "--session-id" not in second["argv"]
    head_two = preamble(message_of(second, "MARK-C10-TWO"), body_two)
    assert head_two == head_one, "the preamble is not fixed"


@pytest.mark.parametrize("variant", ["all", "name_only"])
def test_c10_model_and_identity_are_mapped_exactly(tool: NestedTool, variant: str) -> None:
    tool.overrides["CODE_TASK_MODEL"] = "custom-model-x"
    if variant == "all":
        tool.overrides.update({
            "CODE_TASK_GH_CONFIG_DIR": str(tool.tmp / "bot-gh"),
            "CODE_TASK_GIT_SSH_COMMAND": "ssh -i /keys/bot -o IdentitiesOnly=yes",
            "CODE_TASK_GIT_NAME": "UG Code Bot",
            "CODE_TASK_GIT_EMAIL": "code-bot@example.com",
        })
        expected = {
            "GH_CONFIG_DIR": str(tool.tmp / "bot-gh"),
            "GIT_SSH_COMMAND": "ssh -i /keys/bot -o IdentitiesOnly=yes",
            "GIT_AUTHOR_NAME": "UG Code Bot",
            "GIT_COMMITTER_NAME": "UG Code Bot",
            "GIT_AUTHOR_EMAIL": "code-bot@example.com",
            "GIT_COMMITTER_EMAIL": "code-bot@example.com",
        }
    else:
        tool.overrides["CODE_TASK_GIT_NAME"] = "UG Code Bot"
        expected = {"GIT_AUTHOR_NAME": "UG Code Bot", "GIT_COMMITTER_NAME": "UG Code Bot"}

    tool.send("WEB-7", "Bump the dependency. MARK-C10-ID")
    assert tool.wait("WEB-7")["status"] == "done"

    (record,) = tool.starts()
    assert option_value(record["argv"], "--model") == "custom-model-x"
    assert record["env"] == expected


def test_c10_e5_turn_without_transcript_restarts_with_the_same_id(tool: NestedTool) -> None:
    tool.set_mode("crash")
    session_id = tool.send("UMN-15", "First try. MARK-E5-ONE")["session_id"]
    assert tool.wait("UMN-15")["status"] == "failed"

    tool.set_mode("ok", result="recovered")
    tool.send("UMN-15", "Second try. MARK-E5-TWO")
    done = tool.wait("UMN-15")

    second = tool.starts()[1]
    assert option_value(second["argv"], "--session-id") == session_id
    assert "--resume" not in second["argv"]
    assert done["status"] == "done", done
    assert done["result"] == "recovered"


# --------------------------------------------------------------------------------------------
# C11 - failures (criterion 11)
# --------------------------------------------------------------------------------------------


def assert_rejected(tool: NestedTool, result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0, result.stdout
    output = result.stdout + result.stderr
    assert "Traceback" not in output and "No module named" not in output, output
    lines = output.strip().splitlines()
    assert len(lines) == 1, f"expected a one-line message, got {lines!r}"
    assert tool.starts() == [], "a nested turn was started"


@pytest.mark.parametrize("key", ["umn-14", "UMN14", "UMN-", "1UMN-2", "U-1", "UMN-1x"])
def test_c11_e6_invalid_key_is_rejected(tool: NestedTool, key: str) -> None:
    message = tool.message("Do the change. MARK-C11-KEY")
    assert_rejected(tool, tool.run("send", "--task", key, "--message-file", str(message)))
    assert_rejected(tool, tool.run("wait", "--task", key, "--max-seconds", "1"))


@pytest.mark.parametrize("problem", ["missing_file", "empty_file", "missing_workspace"])
def test_c11_bad_send_input_starts_nothing(tool: NestedTool, problem: str) -> None:
    message = tool.message("Do the change. MARK-C11-INPUT")
    if problem == "missing_file":
        message = tool.tmp / "absent.txt"
    elif problem == "empty_file":
        message = tool.message("", name="empty.txt")
    else:
        tool.overrides["CODE_TASK_WORKSPACE"] = str(tool.tmp / "no-such-workspace")

    assert_rejected(tool, tool.run("send", "--task", "UMN-20", "--message-file", str(message)))
    # Nothing was started, so the task still never had a turn.
    tool.overrides.pop("CODE_TASK_WORKSPACE", None)
    assert_rejected(tool, tool.run("wait", "--task", "UMN-20", "--max-seconds", "1"))


def test_c11_wait_for_a_task_that_never_ran_is_rejected(tool: NestedTool) -> None:
    assert_rejected(tool, tool.run("wait", "--task", "UMN-404", "--max-seconds", "1"))


def test_c11_send_while_a_turn_runs_is_rejected(tool: NestedTool) -> None:
    tool.set_mode("hang", seconds=60)
    tool.send("UMN-21", "Long change. MARK-C11-BUSY")
    tool.wait_for_starts(1)

    result = tool.run("send", "--task", "UMN-21", "--message-file",
                      str(tool.message("Another change. MARK-C11-BUSY-2", name="second.txt")))

    assert result.returncode != 0, result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert len((result.stdout + result.stderr).strip().splitlines()) == 1
    time.sleep(1)
    assert len(tool.starts()) == 1, "a second nested turn was started"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("exit_nonzero", "boom-stderr-tail"), ("invalid_json", "invalid json"),
     ("is_error", "is_error")],
)
def test_c11_failed_turn_reports_the_reason(tool: NestedTool, mode: str, expected: str) -> None:
    tool.set_mode(mode)
    tool.send("UMN-22", "Change. MARK-C11-FAIL")

    result = tool.wait("UMN-22")

    assert result["status"] == "failed", result
    reason = str(result.get("reason", ""))
    if expected == "invalid json":
        assert expected in reason.casefold(), reason
    else:
        assert expected in reason, reason


def test_c11_turn_outliving_the_nested_timeout_is_killed(tool: NestedTool) -> None:
    tool.overrides["CODE_TASK_NESTED_TIMEOUT_SECONDS"] = "2"
    tool.set_mode("hang", seconds=60)
    tool.send("UMN-23", "Slow change. MARK-C11-TIMEOUT")
    (record,) = tool.wait_for_starts(1)

    began = time.monotonic()
    result = tool.wait("UMN-23", seconds=40)

    assert result["status"] == "failed", result
    assert "timeout" in str(result.get("reason", "")).casefold(), result
    assert time.monotonic() - began < 30
    deadline = time.monotonic() + 5
    while alive(record["pid"]) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(record["pid"]), "the nested Claude process was not killed"


def test_c11_vanished_turn_is_interrupted(tool: NestedTool) -> None:
    tool.set_mode("hang", seconds=60)
    tool.send("UMN-24", "Change. MARK-C11-GONE")
    (record,) = tool.wait_for_starts(1)

    tool.kill_tree(record)
    deadline = time.monotonic() + 5
    while alive(record["pid"]) and time.monotonic() < deadline:
        time.sleep(0.1)
    result = tool.wait("UMN-24", seconds=10)

    assert result["status"] == "interrupted", result


def test_c11_wait_returns_running_after_max_seconds(tool: NestedTool) -> None:
    tool.set_mode("hang", seconds=60)
    tool.send("UMN-25", "Change. MARK-C11-RUNNING")
    tool.wait_for_starts(1)

    began = time.monotonic()
    result = tool.wait("UMN-25", seconds=1)
    elapsed = time.monotonic() - began

    assert result["status"] == "running", result
    assert 0.8 <= elapsed < 15, elapsed


def test_c11_max_seconds_above_limit_is_clamped_and_below_one_rejected(tool: NestedTool) -> None:
    # Ruling R7: above 570 is clamped (no error); values below 1 exit non-zero.
    tool.set_mode("ok", result="clamped")
    tool.send("UMN-26", "Change. MARK-C11-CLAMP")
    result = tool.run("wait", "--task", "UMN-26", "--max-seconds", "600", timeout=120)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "done"
    for value in ("0", "-3"):
        rejected = tool.run("wait", "--task", "UMN-26", "--max-seconds", value)
        assert rejected.returncode != 0, rejected.stdout
        assert "Traceback" not in rejected.stdout + rejected.stderr


if __name__ == "__main__":
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "code_task_acceptance_runner",
        Path(__file__).with_name("test_jira_code_tasks_acceptance.py"),
    )
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    sys.exit(runner.sensitivity_main(sys.argv[1:], Path(__file__)))
