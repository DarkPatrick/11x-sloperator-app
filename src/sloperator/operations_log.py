# ruff: noqa: RUF001
"""Independent durable Slack operations collector. No LLM is used to summarize logs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from slack_sdk.errors import SlackApiError

from sloperator.claude_usage import read_usage
from sloperator.config import Settings
from sloperator.operations_cron import wrap_crontab
from sloperator.operations_slack import ObservedSlackClient
from sloperator.operations_store import OperationsStore, outcome, redact

LOGGER = logging.getLogger(__name__)


def concise(text: str, limit: int = 230) -> str:
    text = redact(text)
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith(("🧭", "📚", "SLOPERATOR_ARTIFACT:"))
    ]
    text = " ".join(lines)
    text = re.sub(r"<@[A-Z0-9]+(?:\|[^>]+)?>|<![^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "'")


def render_event(event: dict[str, Any]) -> str:
    icon, label = outcome(event["status"], event["detail"])
    source = concise(event["source"], 100)
    detail = concise(event["detail"], 1000 if event["status"] == "usage" else 230)
    reference = event["reference"]
    thread = re.fullmatch(r"([CDG][A-Z0-9]+)/([0-9]+\.[0-9]+)", reference)
    link = ""
    if thread:
        channel, timestamp = thread.groups()
        link = f" · <https://slack.com/archives/{channel}/p{timestamp.replace('.', '')}|тред>"
    return f"{icon} *{slack_escape(source)}* — {label} ({event['created_at'][11:19]} UTC){link}" + (
        f"\n{slack_escape(detail)}" if detail else ""
    )


class OperationsCollector:
    def __init__(self, settings: Settings, client: Any, channel: str):
        self.settings = settings
        self.client = client
        self.channel = channel
        self.store = OperationsStore(settings.database_path)
        self.directory = settings.database_path.resolve().parent / "operations"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.started = time.time()

    async def publish_pending(self) -> int:
        events = await asyncio.to_thread(self.store.pending, 6)
        if not events:
            return 0
        # Usage is a distinct post, never buried in a lifecycle digest.
        for index, event in enumerate(events):
            if event["status"] == "usage" or event["source"] == "Claude /usage":
                events = events[:index] if index else events[:1]
                break
        # Raw evidence is private on the server. Never invent a public URL for a
        # loopback-only admin UI. Every digest gives an actual existing file path.
        ids = [event["id"] for event in events]
        path = self.directory / "deliveries" / f"{ids[0]}-{ids[-1]}.json"
        path.parent.mkdir(exist_ok=True, mode=0o700)
        path.write_text(json.dumps(events, ensure_ascii=False, indent=2))
        groups: list[list[dict[str, Any]]] = []
        for event in events:
            if (
                groups
                and event["status"] == "info"
                and groups[-1][0]["status"] == "info"
                and event["source"] == groups[-1][0]["source"]
            ):
                groups[-1].append(event)
            else:
                groups.append([event])
        message = "\n\n".join(
            render_event(group[-1])
            + (f"\nВсего событий: {len(group)}; подробности в логе." if len(group) > 1 else "")
            for group in groups
        )
        message += f"\n\nСырой лог на сервере: `{socket.gethostname()}:{path.resolve()}`"
        await self.client.chat_postMessage(
            channel=self.channel,
            text=message,
            mrkdwn=True,
            unfurl_links=False,
            unfurl_media=False,
            parse="none",
            client_msg_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"operations:{self.channel}:{ids}")),
        )
        await asyncio.to_thread(self.store.delivered, ids)
        return len(events)

    async def usage(self) -> None:
        # Wall-clock slots survive restarts; no empty/zero usage on errors.
        slot = str(int(time.time() // 1800))
        if self.store.cursor("usage-slot") == slot:
            return
        try:
            usage = await read_usage(
                self.settings.claude_cli, model=self.settings.claude_model, cwd=self.directory
            )
            detail = (
                f"Сессия: использовано {usage.session_used_percent}%, "
                f"осталось {usage.session_remaining_percent}%; "
                f"сброс {usage.session_reset_text or 'не указан'}. "
                f"Неделя: использовано {usage.week_used_percent}%, "
                f"осталось {usage.week_remaining_percent}%; сброс {usage.week_reset_text}."
            )
            self.store.emit("Claude", "usage", detail, key=f"usage:{slot}")
        except Exception as error:
            self.store.emit(
                "Claude /usage",
                "failed",
                f"Не удалось получить свежие лимиты: {type(error).__name__}. "
                "Значения неизвестны; следующая проверка через 30 минут.",
                key=f"usage:{slot}",
            )
            (self.directory / f"usage-{slot}.log").write_text(redact(repr(error)))
        self.store.checkpoint("usage-slot", slot)

    def ingest_spool(self) -> None:
        directory = self.directory / "spool"
        directory.mkdir(exist_ok=True, mode=0o700)
        for path in sorted(directory.glob("*.json")):
            event = json.loads(path.read_text())
            self.store.emit(
                event["source"],
                event["status"],
                event.get("detail", ""),
                event.get("reference", ""),
                key="spool:" + event["key"],
            )
            # Keep the start until its matching finish so SIGKILL/reboot is observable.
            run_id = event.get("run_id")
            if run_id:
                active = self.directory / "active-crons"
                active.mkdir(exist_ok=True, mode=0o700)
                marker = active / f"{run_id}.json"
                if event["status"] == "running":
                    marker.write_text(json.dumps(event))
                else:
                    marker.unlink(missing_ok=True)
            path.unlink()
        active = self.directory / "active-crons"
        for path in active.glob("*.json"):
            event = json.loads(path.read_text())
            if time.time() - event["started"] < 30:
                continue
            proc = Path(f"/proc/{event['pid']}/cmdline")
            # Verify executable, not just PID existence (PIDs are reused).
            alive = proc.exists() and b"sloperator.operations_cron" in proc.read_bytes()
            if not alive:
                self.store.emit(
                    event["source"],
                    "interrupted",
                    "Процесс исчез без результата (остановка/перезагрузка).",
                    event["reference"],
                    key="lost-cron:" + event["run_id"],
                )
                path.unlink()

    def ensure_cron_coverage(self) -> list[str]:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        if result.returncode and "no crontab" not in result.stderr:
            raise RuntimeError("Не удалось прочитать пользовательский crontab")
        original = result.stdout
        updated, names = wrap_crontab(original, self.directory / "cron-specs", Path(sys.executable))
        if updated.strip() != original.strip():
            backup = self.directory / f"crontab-before-{time.time_ns()}.txt"
            backup.write_text(original)
            # Recheck before replacing to avoid overwriting a concurrent human edit.
            current = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
            if current.stdout != original:
                raise RuntimeError("Crontab изменился во время проверки; повтор через минуту")
            subprocess.run(
                ["crontab", "-"],
                input=updated,
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            self.store.emit(
                "Покрытие cron",
                "info",
                f"Наблюдение включено для {len(names)} задач: " + ", ".join(names),
                str(backup),
            )
        return names

    def ingest_journal(self) -> None:
        cursor = self.store.cursor("journal")
        command = [
            "journalctl",
            "--no-pager",
            "-o",
            "json",
            "--output-fields=MESSAGE,SYSLOG_IDENTIFIER,_SYSTEMD_UNIT,UNIT,PRIORITY",
        ]
        # Read a single journal cursor, filter records below. Mixing -u with '+'
        # field matches is not valid journalctl syntax and loses CRON records.
        command += ["--after-cursor", cursor] if cursor else ["--since", "now"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        if result.returncode and cursor:
            # Journal vacuum/reboot can invalidate a cursor. Resume from the last
            # persisted time; event keys deduplicate the overlapping boundary.
            since = self.store.cursor("journal-time") or "1 hour ago"
            result = subprocess.run(
                [*command[:-2], "--since", since], capture_output=True, text=True, timeout=20
            )
            self.store.emit(
                "systemd journal",
                "warning",
                "Курсор журнала недоступен; чтение восстановлено по времени.",
                key="journal-reset:" + cursor,
            )
        if result.returncode:
            raise RuntimeError("journalctl не смог прочитать курсор: " + result.stderr[-200:])
        for line in result.stdout.splitlines():
            item = json.loads(line)
            position = item.get("__CURSOR")
            message = str(item.get("MESSAGE", ""))
            unit = str(
                item.get("UNIT")
                or item.get("_SYSTEMD_UNIT")
                or item.get("SYSLOG_IDENTIFIER", "systemd")
            )
            if re.search(r"\) LIST \(", message):
                # Reading the crontab for the admin UI is not a job execution.
                # The raw record remains in journald; avoid a post every 5 seconds.
                pass
            elif (
                unit == "sloperator-operations.service"
                and item.get("SYSLOG_IDENTIFIER") != "systemd"
            ):
                # Avoid log-delivery errors feeding themselves forever.
                pass
            elif " CMD (" in message and "sloperator.operations_cron" in message:
                pass  # wrapper owns accurate start/end; CRON only means dispatch
            else:
                target_unit = unit.startswith(("sloperator", "ug-ai-analyst"))
                match = re.search(
                    r"\b(INFO|WARNING|ERROR|CRITICAL) ([\w.]+): (.*)", message
                )
                if match and target_unit:
                    level, source, body = match.groups()
                    status = {"INFO": "info", "WARNING": "warning"}.get(level, "failed")
                    # Routine traffic is batched, but still present in raw journal evidence.
                    if body.startswith(
                        ("Ignoring message from unauthorized", "Slack map synchronized")
                    ):
                        source = "Служебные события " + source
                    self.store.emit(
                        source,
                        status,
                        body,
                        f"journalctl -u {unit}",
                        key="journal:" + str(position),
                    )
                elif target_unit and item.get("SYSLOG_IDENTIFIER") == "systemd":
                    status = (
                        "failed" if re.search(r"error|fail|traceback", message, re.I) else "info"
                    )
                    self.store.emit(
                        unit,
                        status,
                        message,
                        f"journalctl -u {unit}",
                        key="journal:" + str(position),
                    )
                elif re.match(r"^\(egor\) CMD \(", message):
                    # Only this user's unwrapped jobs belong to Sloperator's scope.
                    # System cron entries such as root's debian-sa1 stay in journald.
                    self.store.emit(
                        unit,
                        "info",
                        message,
                        f"journalctl -u {unit}",
                        key="journal:" + str(position),
                    )
            if position:
                self.store.checkpoint("journal", position)
                timestamp = int(item.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000
                if timestamp:
                    self.store.checkpoint("journal-time", f"@{timestamp:.6f}")
        if not cursor and not result.stdout.strip():
            # Establish an actual cursor even when no relevant source emitted yet.
            result = subprocess.run(
                ["journalctl", "-n", "1", "-o", "json", "--no-pager"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.stdout.strip():
                self.store.checkpoint(
                    "journal", json.loads(result.stdout.splitlines()[-1])["__CURSOR"]
                )

    def ingest_retry_logs(self) -> None:
        # cron_retry launches detached processes: their later start/end cannot be
        # seen by the original cron wrapper. Observe the common retry log protocol.
        for path in (self.settings.agent_workspace / "scripts" / "logs").glob("*.cron.*.log"):
            name = "file:" + str(path)
            stat = path.stat()
            stored = self.store.cursor(name)
            inode, offset = json.loads(stored) if stored else (stat.st_ino, stat.st_size)
            if inode != stat.st_ino or offset > stat.st_size:
                offset = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                for _ in range(2000):
                    before = stream.tell()
                    raw = stream.readline()
                    if not raw or not raw.endswith(b"\n"):
                        stream.seek(before)
                        break
                    line = raw.decode(errors="replace")
                    match = re.search(r"\[cron_retry:([^]]+)\] (.*)", line)
                    if match:
                        label, body = match.groups()
                        status = "info"
                        if body.startswith("running:"):
                            status, body = "running", "Запущена попытка cron/retry"
                        elif "child exited rc=" in body:
                            status = "completed" if "rc=0" in body else "failed"
                        elif "not the scheduled fire" in body:
                            status = "skipped"
                        self.store.emit(
                            label,
                            status,
                            body,
                            str(path),
                            key=f"retry:{path}:{stat.st_ino}:{before}",
                        )
                self.store.checkpoint(name, json.dumps([stat.st_ino, stream.tell()]))

    def ingest_subagents(self) -> None:
        # Only children of recorded Sloperator sessions; never scrape unrelated
        # interactive Claude sessions or user prompts.
        with self.store.connect() as connection:
            parents = {
                str(row[0])
                for row in connection.execute(
                    "SELECT external_session_id FROM scheduled_agent_runs "
                    "WHERE external_session_id IS NOT NULL "
                    "AND (status='running' OR updated_at >= datetime('now','-1 day')) "
                    "UNION SELECT external_session_id FROM agent_sessions "
                    "WHERE external_session_id IS NOT NULL "
                    "AND (status='running' OR updated_at >= datetime('now','-1 day'))"
                )
            }
        workspace = str(self.settings.agent_workspace.resolve()).replace("/", "-")
        root = Path.home() / ".claude" / "projects" / workspace
        since = float(self.store.cursor("installed-at") or self.started)
        for parent in parents:
            for path in (root / parent / "subagents").glob("agent-*.jsonl"):
                if path.stat().st_mtime < since:
                    continue
                key = "subagent-file:" + str(path)
                offset = int(self.store.cursor(key) or 0)
                if offset > path.stat().st_size:
                    offset = 0
                if not offset:
                    self.store.emit(
                        "Claude subagent " + path.stem,
                        "running",
                        "Дочерний агент сессии " + parent,
                        str(path),
                        key="subagent-start:" + str(path),
                    )
                with path.open("rb") as stream:
                    stream.seek(offset)
                    for _ in range(2000):
                        before = stream.tell()
                        raw = stream.readline()
                        if not raw or not raw.endswith(b"\n"):
                            stream.seek(before)
                            break
                        item = json.loads(raw)
                        message = item.get("message", {})
                        if (
                            message.get("role") == "assistant"
                            and message.get("stop_reason") == "end_turn"
                        ):
                            text = "\n".join(
                                part.get("text", "")
                                for part in message.get("content", [])
                                if isinstance(part, dict) and part.get("type") == "text"
                            )
                            self.store.emit(
                                "Claude subagent " + path.stem,
                                "returned",
                                text,
                                str(path),
                                key=f"subagent-result:{path}:{before}",
                            )
                    self.store.checkpoint(key, str(stream.tell()))

    async def run(self) -> None:
        self.store.initialize()
        if self.store.cursor("installed-at") is None:
            self.store.checkpoint("installed-at", str(time.time()))
        self.store.emit(
            "Журнал Sloperator",
            "info",
            "Сборщик запущен; события сохраняются "
            "до успешной доставки. Claude /usage — каждые 30 минут.",
        )
        tasks = [asyncio.create_task(self.collect_loop()), asyncio.create_task(self.usage_loop())]
        try:
            while True:
                try:
                    delivered = await self.publish_pending()
                    await asyncio.sleep(1.2 if delivered else 10)
                except SlackApiError as error:
                    delay = int(error.response.headers.get("Retry-After", "15"))
                    LOGGER.error(
                        "Slack log delivery failed (%s); queue retained",
                        error.response.get("error"),
                    )
                    await asyncio.sleep(max(2, min(delay, 300)))
                except Exception:
                    LOGGER.exception("Slack log delivery failed; queue retained")
                    await asyncio.sleep(15)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def collect_loop(self) -> None:
        last_coverage = 0.0
        while True:
            for name, collect in (
                ("cron spool", self.ingest_spool),
                ("systemd", self.ingest_journal),
                ("cron retries", self.ingest_retry_logs),
                ("child agents", self.ingest_subagents),
            ):
                try:
                    await asyncio.to_thread(collect)
                except Exception as error:
                    LOGGER.exception("Collector failed: %s", name)
                    self.store.emit(
                        "Сборщик " + name,
                        "failed",
                        str(error),
                        key=f"collector:{name}:{int(time.time() // 1800)}",
                    )
            if time.time() - last_coverage >= 60:
                try:
                    await asyncio.to_thread(self.ensure_cron_coverage)
                except Exception as error:
                    self.store.emit(
                        "Покрытие cron",
                        "failed",
                        str(error),
                        key=f"coverage:{int(time.time() // 1800)}",
                    )
                last_coverage = time.time()
            await asyncio.sleep(10)

    async def usage_loop(self) -> None:
        while True:
            try:
                await self.usage()
            except Exception:
                LOGGER.exception("Usage collector failed")
            await asyncio.sleep(30)


async def serve() -> None:
    settings = Settings.from_environment()
    channel = os.environ.get("SLOPERATOR_LOG_CHANNEL", "").strip()
    if not channel:
        raise RuntimeError("SLOPERATOR_LOG_CHANNEL is required")
    collector = OperationsCollector(
        settings,
        ObservedSlackClient(token=settings.bot_token),
        channel,
    )
    task = asyncio.create_task(collector.run())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await task


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
