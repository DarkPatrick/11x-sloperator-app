"""Bounded, read-only context gathering and synchronous command waiting for agents."""
# ruff: noqa: ASYNC109, ASYNC240

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

WORKSPACE = Path("/home/egor/projects/ug-ai-analyst")
PYTHON = WORKSPACE / ".venv/bin/python"
JIRA = WORKSPACE / ".claude/jira/jira_issue.py"
CONFLUENCE = WORKSPACE / ".claude/confluence/confluence_page.py"
ISSUE = re.compile(r"UMN-\d+\Z")
PAGE = re.compile(r"\d+\Z")
MAX_TAIL = 3000
CLI = "/home/egor/projects/11x-sloperator-app/.venv/bin/python -m sloperator.experiment_agent_tools"


def agent_instructions(page_id: str, *issue_keys: str) -> str:
    """Give an agent one bounded initial read and one way to await long commands."""
    issues = " ".join(f"--issue {key}" for key in issue_keys)
    return f"""Initial bounded context read (run once before further investigation):
`{CLI} context --page {page_id} {issues}`
This gathers the named Jira issues and exactly one Confluence page with service-account helpers.
It prints brief facts and snapshot paths. Read needed passages with
`{CLI} excerpt --file <text_path> --find <heading-or-id>`; repeat for a few exact headings or IDs.
Never dump the whole page into the conversation. Re-fetch immediately before writes and after
publication.
For long local calculations, run `{CLI} wait --timeout 540 -- <command> <args>` as one synchronous
Bash call with a 600000 ms Bash timeout. The wrapper saves full logs and returns a bounded final
result. Do not launch background jobs, sleep, or poll for progress through repeated Bash calls.
"""


async def _capture(command: list[str], timeout: int) -> str:
    process = await asyncio.create_subprocess_exec(
        *command, cwd=WORKSPACE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError(f"Timed out after {timeout}s: {command[1].split('/')[-1]}") from None
    if process.returncode:
        raise RuntimeError(
            f"{command[1].split('/')[-1]} failed ({process.returncode}): "
            + stderr.decode(errors="replace")[-MAX_TAIL:]
        )
    return stdout.decode()


def _issue_brief(data: dict[str, Any]) -> dict[str, Any]:
    fields = data.get("fields") or {}
    return {
        "key": data.get("key"),
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "assignee_account_id": (fields.get("assignee") or {}).get("accountId"),
        "start_date": fields.get("customfield_10312"),
        "due_date": fields.get("duedate"),
        "parent": (fields.get("parent") or {}).get("key"),
        "updated": fields.get("updated"),
    }


def _tail(path: Path) -> str:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - MAX_TAIL * 4))
        return stream.read().decode("utf-8", errors="replace")[-MAX_TAIL:]


async def gather(keys: list[str], page_id: str, timeout: int) -> dict[str, Any]:
    """Fetch only named Jira issues and one Confluence page; keep full bodies on disk."""
    if not keys or any(not ISSUE.fullmatch(key) for key in keys):
        raise ValueError("Every Jira key must match UMN-<number>")
    if not PAGE.fullmatch(page_id):
        raise ValueError("Confluence page ID must be numeric")
    artifact_dir = Path(tempfile.mkdtemp(prefix="experiment_context_", dir=WORKSPACE / "output"))
    commands = [
        [str(PYTHON), str(JIRA), "get", key, "--as-bot", "--json",
         "--fields", "summary,status,assignee,customfield_10312,duedate,parent,updated"]
        for key in keys
    ]
    commands.append([
        str(PYTHON), str(CONFLUENCE), "fetch", page_id, "--as-bot",
        "--out-dir", str(artifact_dir),
    ])
    results = await asyncio.gather(*(_capture(command, timeout) for command in commands))
    issues = []
    for key, raw in zip(keys, results[:-1], strict=True):
        (artifact_dir / f"{key}.json").write_text(raw, encoding="utf-8")
        issues.append(_issue_brief(json.loads(raw)))
    page = json.loads(results[-1])
    page_text = Path(page["text_path"])
    return {
        "issues": issues,
        "page": {
            "id": page["page_id"], "title": page["title"],
            "version": page["version"], "text_bytes": page_text.stat().st_size,
            "text_path": str(page_text), "storage_path": page["storage_path"],
            "json_path": page["json_path"],
        },
        "artifact_dir": str(artifact_dir),
        "instruction": (
            "Read only the sections needed from the saved page; do not print its full body."
        ),
    }


async def wait_for(command: list[str], timeout: int) -> dict[str, Any]:
    """Wait once and return bounded output while preserving full logs on disk."""
    if not command:
        raise ValueError("Command is required")
    artifact_dir = Path(tempfile.mkdtemp(prefix="experiment_command_", dir=WORKSPACE / "output"))
    stdout_path = artifact_dir / "stdout.log"
    stderr_path = artifact_dir / "stderr.log"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=WORKSPACE, stdout=stdout, stderr=stderr,
            start_new_session=True,
        )
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except TimeoutError:
            import os
            import signal

            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            timed_out = True
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout_tail": _tail(stdout_path),
        "stderr_tail": _tail(stderr_path),
        "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
    }


def excerpt(path: str, needle: str, radius: int = 12) -> dict[str, Any]:
    """Return a bounded window around exact terms in a saved context artifact."""
    target = Path(path).resolve()
    if not target.is_relative_to((WORKSPACE / "output").resolve()) or target.suffix != ".txt":
        raise ValueError("Excerpt file must be a saved text artifact under output/")
    if not needle.strip():
        raise ValueError("Search term is required")
    lines = target.read_text(encoding="utf-8").splitlines()
    matches = [index for index, line in enumerate(lines) if needle.casefold() in line.casefold()]
    windows = []
    for index in matches[:3]:
        start = max(0, index - radius)
        end = min(len(lines), index + radius + 1)
        windows.append({"line": index + 1, "text": "\n".join(lines[start:end])[:4000]})
    return {"matches": len(matches), "windows": windows, "file": str(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    context = sub.add_parser("context")
    context.add_argument("--page", required=True)
    context.add_argument("--issue", action="append", required=True)
    context.add_argument("--timeout", type=int, default=90)
    waiting = sub.add_parser("wait")
    waiting.add_argument("--timeout", type=int, default=540)
    waiting.add_argument("command", nargs=argparse.REMAINDER)
    snippet = sub.add_parser("excerpt")
    snippet.add_argument("--file", required=True)
    snippet.add_argument("--find", required=True)
    args = parser.parse_args()
    try:
        if args.action == "context":
            result = asyncio.run(gather(args.issue, args.page, args.timeout))
        elif args.action == "wait":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            result = asyncio.run(wait_for(command, args.timeout))
        else:
            result = excerpt(args.file, args.find)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("exit_code", 0) == 0 else 1
    except (ValueError, RuntimeError, OSError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
