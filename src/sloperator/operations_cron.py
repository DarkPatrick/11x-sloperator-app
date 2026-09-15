# ruff: noqa: RUF001
"""Observe whole cron commands without changing their environment, redirection or exit code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


def cron_entries(text: str) -> list[tuple[int, str, str, str, str]]:
    entries = []
    name = "user-cron"
    for index, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if line.startswith("# >>> "):
            name = line.removeprefix("# >>> ").removesuffix(" >>>")
        if line.startswith("# <<< "):
            name = "user-cron"
        prefix = ""
        if line.startswith("# sloperator-disabled: "):
            prefix = "# sloperator-disabled: "
            line = line.removeprefix(prefix)
        if not line or line.startswith("#") or re.match(r"\w+\s*=", line):
            continue
        fields = line.split(maxsplit=1 if line.startswith("@") else 5)
        if len(fields) not in {2, 6}:
            continue
        schedule, command = " ".join(fields[:-1]), fields[-1]
        entries.append((index, name, schedule, command, prefix))
    return entries


def command_spec(command: str) -> Path | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if "sloperator.operations_cron" in tokens and "--spec" in tokens:
        return Path(tokens[tokens.index("--spec") + 1])
    return None


def unwrap_command(command: str) -> str:
    spec = command_spec(command)
    if spec:
        try:
            return str(json.loads(spec.read_text())["command"])
        except (OSError, KeyError, ValueError):
            pass
    return command


def wrap_crontab(text: str, directory: Path, python: Path) -> tuple[str, list[str]]:
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = text.splitlines()
    names = []
    for index, name, schedule, command, prefix in cron_entries(text):
        names.append(name)
        if command_spec(command):
            continue
        # cron interprets unescaped % as stdin BEFORE invoking the shell. Keep
        # those jobs unchanged and report a coverage error rather than break them.
        if re.search(r"(?<!\\)%", command):
            raise ValueError(f"Cron {name} uses % stdin; explicit instrumentation required")
        key = hashlib.sha256((name + command).encode()).hexdigest()[:20]
        spec = directory / f"{key}.json"
        spec.write_text(
            json.dumps(
                {"name": name, "command": command, "directory": str(directory.parent)},
                ensure_ascii=False,
            )
        )
        os.chmod(spec, 0o600)
        wrapper = shlex.join([str(python), "-m", "sloperator.operations_cron", "--spec", str(spec)])
        lines[index] = prefix + schedule + " " + wrapper
    return "\n".join(lines) + "\n", names


def source_logs(command: str) -> list[Path]:
    tokens = shlex.split(command)
    paths = []
    for index, token in enumerate(tokens):
        if token in {">", ">>", "2>", "2>>", "--log-out", "--log-err"} and index + 1 < len(tokens):
            paths.append(Path(tokens[index + 1]))
        if token.endswith(".py") and Path(token).name != "cron_retry.py":
            script = Path(token)
            paths.append(script.parent / "logs" / f"{script.stem}.jsonl")
    return list(dict.fromkeys(p for p in paths if p.is_absolute()))


def spool(directory: Path, event: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = str(uuid.uuid4())
    event["key"] = key
    temp = directory / f".{key}.tmp"
    temp.write_text(json.dumps(event, ensure_ascii=False))
    temp.replace(directory / f"{time.time_ns()}-{key}.json")


def execute(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text())
    directory = Path(spec["directory"])
    run_id = str(uuid.uuid4())
    logs = directory / "cron-runs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = logs / f"{run_id}.log"
    sources = source_logs(spec["command"])
    offsets = {p: p.stat().st_size if p.exists() else 0 for p in sources}
    base = {
        "source": spec["name"],
        "reference": str(raw),
        "run_id": run_id,
        "pid": os.getpid(),
        "started": time.time(),
    }
    spool(directory / "spool", {**base, "status": "running", "detail": "Начат cron"})
    process = None
    try:
        with raw.open("wb") as output:
            # Cron already supplies HOME, PATH and the per-line environment.
            # Start in that inherited cwd; the original command retains its cd.
            process = subprocess.Popen(
                [os.environ.get("SHELL", "/bin/bash"), "-c", spec["command"]],
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            def forward(signum: int, _frame: Any) -> None:
                if process is not None:
                    os.killpg(process.pid, signum)

            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, forward)
            rc = process.wait()
        with raw.open("ab") as output:
            for path, offset in offsets.items():
                if not path.exists():
                    continue
                with path.open("rb") as original:
                    original.seek(offset if path.stat().st_size >= offset else 0)
                    output.write(f"\n--- {path} ---\n".encode())
                    # Leave large complete originals in their existing locations.
                    output.write(original.read(256_000))
        with raw.open("rb") as evidence:
            evidence.seek(max(0, raw.stat().st_size - 12000))
            content = evidence.read().decode(errors="replace")
        status = "completed" if rc == 0 else "failed"
        if "not the scheduled fire" in content:
            status = "skipped"
        if re.search(r'"status"\s*:\s*"(?:data_unavailable|failed|error)"', content):
            status = "failed"
        lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.startswith("---")
        ]
        errors = [
            line
            for line in lines
            if re.search(r"error|failed|exception|unavailable|skip|retry|incomplete", line, re.I)
        ]
        summary = " | ".join((errors or lines)[-3:])
        for line in reversed(lines):
            try:
                result = json.loads(line)
            except ValueError:
                continue
            if not isinstance(result, dict):
                continue
            fields = {
                "status": "результат",
                "candidate_builds": "проверено сборок",
                "findings": "найдено отклонений",
                "pending_sync": "ожидают синхронизации",
                "error": "ошибка",
                "exit_code": "код выхода",
            }
            facts = [f"{label}: {result[key]}" for key, label in fields.items() if key in result]
            if facts:
                summary = "; ".join(facts)
                break
        detail = f"Код выхода {rc}; {time.time() - base['started']:.0f} с. {summary}"
        spool(directory / "spool", {**base, "status": status, "detail": detail})
        return rc if rc >= 0 else 128 - rc
    except BaseException as error:
        spool(
            directory / "spool",
            {**base, "status": "failed", "detail": f"{type(error).__name__}: {error}"},
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(execute(args.spec))


if __name__ == "__main__":
    main()
