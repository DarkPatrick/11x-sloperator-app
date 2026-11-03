"""Tag the operator under failed-DAG alerts in #a-airflow-monitoring that are theirs.

analytics_bot posts one `:x: DAG Failed` (or `Critical DAG Failed`) message per failed task.
This trigger hands the DAG, run and task to the deterministic `ug-ai-analyst` resolver
`scripts/airflow_alert_owner.py`, which reads the DAG source and the task log through the
Airflow API and the failed dbt models' owners from the dbt repository, and answers whether the
alert belongs to the operator:

* a plain DAG whose `access_control` grants the configured role (`User_UG_Monetisation`);
* a dbt task where a failed model's `meta.owner` is the configured owner (`Egor Semin`).

On a match the bot replies in the alert's thread with a mention and the one-line reason. No
agent is involved and nothing is posted when the alert is someone else's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.config import Settings
from sloperator.store import EventStore

LOGGER = logging.getLogger(__name__)

RESOLVER = "scripts/airflow_alert_owner.py"
EX_TEMPFAIL = 75
HANDLED_LIMIT = 500
# The history sync replays messages it has not archived yet — e.g. the whole channel right after
# the bot joins it. An alert this old is someone's past incident, not one to tag now.
MAX_ALERT_AGE_SECONDS = 6 * 3_600

DAG_RE = re.compile(r"\*Dag\*:\s*(?P<dag>[\w.\-]+)")
TASK_RE = re.compile(r"\*Task\*:\s*(?P<task>[\w.\-]+)")
LOGS_RE = re.compile(r"/dags/(?P<dag>[^/|>]+)/runs/(?P<run>[^/|>]+)/tasks/(?P<task>[^/|>]+)")


class AirflowOwnerError(RuntimeError):
    """The resolver could not reach a verdict."""


@dataclass(frozen=True, slots=True)
class FailedTask:
    """The failed task an alert names."""

    dag: str
    run: str
    task: str


def parse_alert(text: str) -> FailedTask | None:
    """Read DAG, run and task from the alert; the run id is only in the Logs link."""
    if "DAG Failed" not in text:
        return None
    link = LOGS_RE.search(text)
    dag = DAG_RE.search(text)
    task = TASK_RE.search(text)
    if link is None or dag is None or task is None:
        return None
    return FailedTask(dag=dag["dag"], run=unquote(link["run"]), task=task["task"])


def is_airflow_owner_trigger(event: dict[str, Any], settings: Settings) -> bool:
    """Match a top-level failed-DAG alert from analytics_bot in the Airflow channel."""
    text = event.get("text")
    thread_ts = event.get("thread_ts")
    return (
        event.get("channel") == settings.airflow_alert_channel
        and (
            event.get("bot_id") == settings.anomaly_bot_id
            or event.get("user") == settings.anomaly_bot_user_id
        )
        and (not isinstance(thread_ts, str) or thread_ts == event.get("ts"))
        and isinstance(text, str)
        and parse_alert(text) is not None
    )


def reply_text(user_id: str, verdict: dict[str, Any]) -> str:
    """One thread line: the mention and why this alert is the operator's."""
    if verdict.get("kind") == "dbt":
        models = ", ".join(f"`{name}`" for name in verdict.get("owned_models", []))
        return f"<@{user_id}> упала твоя dbt-модель: {models}"
    return (
        f"<@{user_id}> это твой DAG — `access_control` выдаёт "
        f"`{', '.join(verdict.get('access_roles', []))}`"
    )


class AirflowOwnerResponder:
    """Resolve a failed DAG's owner; mention the operator in the thread when it is theirs."""

    TRIGGER_KEY = "airflow-owner"

    def __init__(self, settings: Settings, store: EventStore | None = None) -> None:
        self.settings = settings
        self.store = store
        self.workspace = settings.agent_workspace.resolve()
        self.state_path = settings.database_path.parent / "airflow-owner-state.json"
        self._in_flight: set[str] = set()

    async def handle(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        message_ts = event.get("ts")
        text = event.get("text")
        if not isinstance(message_ts, str) or not isinstance(text, str):
            return
        failed = parse_alert(text)
        if failed is None or message_ts in self._in_flight:
            return
        if time.time() - float(message_ts) > MAX_ALERT_AGE_SECONDS:
            LOGGER.debug("Skipping stale Airflow alert %s", message_ts)
            return
        self._in_flight.add(message_ts)
        try:
            if await asyncio.to_thread(self._handled, message_ts):
                return
            await self._record(message_ts, "running")
            verdict = await self.resolve(failed)
            LOGGER.info(
                "Airflow alert %s %s/%s: mention=%s (%s)",
                message_ts, failed.dag, failed.task, verdict.get("mention"), verdict.get("reason"),
            )
            if verdict.get("mention"):
                await client.chat_postMessage(
                    channel=self.settings.airflow_alert_channel,
                    thread_ts=message_ts,
                    text=reply_text(self.settings.slack_user_id, verdict),
                    unfurl_links=False,
                    unfurl_media=False,
                )
            await asyncio.to_thread(self._mark_handled, message_ts)
            await self._record(
                message_ts,
                "completed",
                f"{failed.dag}/{failed.task}: {verdict.get('reason')}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.exception("Airflow owner check failed for alert %s", message_ts)
            await self._record(message_ts, "failed", str(error)[-1_000:])
        finally:
            self._in_flight.discard(message_ts)

    async def resolve(self, failed: FailedTask) -> dict[str, Any]:
        """Run the repository resolver and return its verdict."""
        python = self.workspace / ".venv" / "bin" / "python"
        script = self.workspace / RESOLVER
        if not python.is_file() or not script.is_file():
            raise AirflowOwnerError(f"{RESOLVER} or its virtualenv is missing in {self.workspace}")
        process = await asyncio.create_subprocess_exec(
            str(python), str(script),
            "--dag", failed.dag, "--run", failed.run, "--task", failed.task,
            "--access-role", self.settings.airflow_owner_access_role,
            "--dbt-owner", self.settings.airflow_owner_dbt_owner,
            cwd=self.workspace,
            env={**os.environ, "UG_SKIP_PREFLIGHT": "1"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=600)
        except (asyncio.CancelledError, TimeoutError):
            if process.returncode is None:
                process.terminate()
                with suppress(ProcessLookupError, TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=10)
            raise
        output = stdout.decode(errors="replace").strip()
        if process.returncode != 0:
            raise AirflowOwnerError(
                f"{RESOLVER} exited {process.returncode}: {output[-1_500:] or 'no output'}"
            )
        try:
            verdict = json.loads(output[output.find("{"):])
        except ValueError as error:
            raise AirflowOwnerError(f"{RESOLVER} printed no verdict: {output[-500:]}") from error
        if not isinstance(verdict, dict):
            raise AirflowOwnerError(f"{RESOLVER} printed no verdict: {output[-500:]}")
        return verdict

    async def _record(self, message_ts: str, status: str, detail: str | None = None) -> None:
        """Report this run to the admin trigger calendar; never fail the check over it."""
        if self.store is None:
            return
        try:
            await asyncio.to_thread(
                self.store.record_pipeline_trigger_run,
                self.TRIGGER_KEY,
                self.settings.airflow_alert_channel,
                message_ts,
                status,
                detail,
            )
        except Exception:  # never fail the check over bookkeeping
            LOGGER.exception("Could not record the airflow owner run %s", message_ts)

    def _load(self) -> list[str]:
        try:
            handled = json.loads(self.state_path.read_text()).get("handled", [])
        except (OSError, ValueError, AttributeError):
            return []
        return [ts for ts in handled if isinstance(ts, str)]

    def _handled(self, message_ts: str) -> bool:
        return message_ts in self._load()

    def _mark_handled(self, message_ts: str) -> None:
        handled = [ts for ts in self._load() if ts != message_ts] + [message_ts]
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"handled": handled[-HANDLED_LIMIT:]}, indent=2))
