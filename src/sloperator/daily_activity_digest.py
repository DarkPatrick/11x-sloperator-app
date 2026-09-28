"""Weekday owner digest with a deterministic no-agent fallback."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import HeadlessAgentRun
from sloperator.automated_session_policy import AUTOMATED_RESPONSE_STYLE
from sloperator.claude_usage import ClaudeUsage, read_usage
from sloperator.config import Settings
from sloperator.experiment_analytics_planner import select_from_jira as select_analytics
from sloperator.experiment_design_planner import select_from_jira as select_design
from sloperator.jira_task_automation import JiraTaskReader, weekly_quota_allows_launch

LOGGER = logging.getLogger(__name__)
TIMEZONE = "Asia/Nicosia"
HOUR = 19
TIMEOUT_SECONDS = 180
JOB_NAME = "daily-activity-digest"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_RE = re.compile(r"\bUMN-\d+\b")
URL_RE = re.compile(r"https://[^\s)>]+")
CONFLUENCE_RE = re.compile(r"https://alice\.mu\.se/[^\s)>]+")
FINAL_STATUSES = {
    "done",
    "готово",
    "in review",
    "на ревью",
    "review",
    "в процессе проверки",
}


DIGEST_PROMPT = f"""\
[claude:claude-opus-5-5]
This is a formatting-only automated Slack digest. Do not investigate, use tools, change files,
or add facts. Rewrite the supplied draft in concise natural Russian while preserving every fact,
order, URL, time estimate, and uncertainty exactly. Return exactly two titled sections with short
bullets. Every Jira URL must remain embedded in its task title and every Confluence URL in the
descriptive phrase such as "подготовлен документ"; never print a bare URL or a separate link label.
Write about your own future actions only in the first person singular: "продолжу", "возьму",
"повторю". Never use "мы", "продолжим", "сделаем", "проверим", or another plural form. Do not
expose technical error details. Return only the Slack-ready message.

{AUTOMATED_RESPONSE_STYLE}
"""


@dataclass(frozen=True, slots=True)
class DigestItem:
    task_key: str
    title: str
    jira_url: str
    note: str
    document_url: str | None = None


@dataclass(frozen=True, slots=True)
class DigestFacts:
    completed: tuple[DigestItem, ...]
    pending: tuple[DigestItem, ...]
    quota_note: str | None = None


class AgentSubmitter(Protocol):
    async def execute_once(
        self,
        text: str,
        timeout_seconds: int,
        *,
        job_name: str = "scheduled-agent",
        workspace: Path | None = None,
        accept_result: Callable[[str], bool] = lambda _: True,
        max_interim_results: int = 2,
    ) -> HeadlessAgentRun: ...


class DigestStore(Protocol):
    def list_scheduled_agent_runs(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def active_jira_task_agent_links(self) -> list[dict[str, Any]]: ...


def next_run_at(now: dt.datetime) -> dt.datetime:
    """Return the next Monday-Friday 19:00 Cyprus wall-clock time."""
    local_now = now.astimezone(ZoneInfo(TIMEZONE))
    candidate = local_now.replace(hour=HOUR, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += dt.timedelta(days=1)
    return candidate


def _run_text(run: dict[str, Any]) -> str:
    return "\n".join(str(message.get("text", "")) for message in run.get("messages", ()))


def _result_text(run: dict[str, Any]) -> str:
    messages = run.get("messages", ())
    return str(messages[-1].get("text", "")) if len(messages) > 1 else ""


def _run_day(run: dict[str, Any], timezone: ZoneInfo) -> dt.date | None:
    value = run.get("updated_at")
    if not value:
        return None
    with suppress(ValueError, TypeError):
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(timezone).date()
    return None


def _task_keys(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(TASK_RE.findall(text)))


def _run_task_keys(run: dict[str, Any]) -> tuple[str, ...]:
    """Extract only the task owned by a run, not incidental task links in its context."""
    job = str(run.get("channel_name", ""))
    text = _run_text(run)
    patterns: tuple[str, ...]
    if job.startswith("jira-task-"):
        patterns = (r"\bJira task (UMN-\d+)\b",)
    elif job.startswith("experiment-design-"):
        patterns = (r"\bcalculation task `?(UMN-\d+)`?",)
    elif job.startswith("experiment-analytics-"):
        patterns = (r"\bAnalytics task `?(UMN-\d+)`?",)
    elif job.startswith("experiment-finalizer-"):
        patterns = (
            r"\bJira task `?(UMN-\d+)`?",
            r"FINALIZATION_STARTED:[^\n]+\|\s*(UMN-\d+)\b",
        )
    else:
        patterns = ()
    for pattern in patterns:
        if match := re.search(pattern, text, re.IGNORECASE):
            return (match.group(1).upper(),)
    return ()


def _document_url(text: str) -> str | None:
    match = CONFLUENCE_RE.search(text)
    return match.group(0).rstrip(".,") if match else None


def _task_title(task: Any | None, task_key: str) -> str:
    return str(getattr(task, "summary", task_key)).strip() or task_key


def _next_weekday_label(now: dt.datetime) -> str:
    local_date = now.astimezone(ZoneInfo(TIMEZONE)).date()
    candidate = local_date + dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += dt.timedelta(days=1)
    if candidate == local_date + dt.timedelta(days=1):
        return "завтра"
    return "в понедельник"


def _valid_completed_run(run: dict[str, Any], task_status: str) -> bool:
    if run.get("status") != "completed":
        return False
    job = str(run.get("channel_name", ""))
    result = str(run.get("messages", [{}])[-1].get("text", "")).strip()
    lowered = result.casefold()
    failure_markers = ("failed:", "не удалось", "no eligible", "task_waiting")
    if any(marker in lowered for marker in failure_markers):
        return False
    if job == "experiment-finalizer-reviewer":
        return "results calculated and published" in lowered
    if job in {"experiment-design-reviewer", "experiment-analytics-reviewer"}:
        return bool(result) and "started:" not in lowered
    if job == "jira-task-reviewer":
        return task_status.casefold() in FINAL_STATUSES
    return False


async def collect_facts(
    settings: Settings,
    store: DigestStore,
    *,
    now: dt.datetime | None = None,
) -> tuple[DigestFacts, ClaudeUsage | None]:
    """Collect authoritative facts; individual unavailable sources degrade gracefully."""
    current = now or dt.datetime.now(dt.UTC)
    timezone = ZoneInfo(TIMEZONE)
    today = current.astimezone(timezone).date()
    runs = await asyncio.to_thread(store.list_scheduled_agent_runs, 500)
    # A previous digest contains task links by design; it is output, not activity evidence.
    runs = [run for run in runs if run.get("channel_name") != JOB_NAME]
    today_runs = [run for run in runs if _run_day(run, timezone) == today]
    reader: JiraTaskReader | None = None
    if settings.jira_username and settings.jira_api_token:
        reader = JiraTaskReader(settings.jira_url, settings.jira_username, settings.jira_api_token)

    snapshots: dict[str, Any] = {}

    async def snapshot(task_key: str) -> Any | None:
        if task_key in snapshots:
            return snapshots[task_key]
        if reader is None:
            return None
        try:
            snapshots[task_key] = await reader.task_snapshot(task_key)
        except Exception:
            LOGGER.warning("Daily digest could not read Jira task %s", task_key)
            return None
        return snapshots[task_key]

    completed: list[DigestItem] = []
    completed_keys: set[str] = set()
    for run in reversed(today_runs):
        for task_key in _run_task_keys(run):
            if task_key in completed_keys:
                continue
            task = await snapshot(task_key)
            status = str(getattr(task, "status", ""))
            if not _valid_completed_run(run, status):
                continue
            page = _document_url(_result_text(run))
            completed.append(
                DigestItem(
                    task_key,
                    _task_title(task, task_key),
                    f"{settings.jira_url.rstrip('/')}/browse/{task_key}",
                    "подготовлен документ" if page else "задача выполнена",
                    page,
                )
            )
            completed_keys.add(task_key)

    pending: list[DigestItem] = []
    pending_keys: set[str] = set()
    today_keys = {task_key for run in today_runs for task_key in _run_task_keys(run)}

    async def add_pending(task_key: str, note: str) -> None:
        if task_key in completed_keys or task_key in pending_keys:
            return
        task = await snapshot(task_key)
        pending.append(
            DigestItem(
                task_key,
                _task_title(task, task_key),
                f"{settings.jira_url.rstrip('/')}/browse/{task_key}",
                note,
            )
        )
        pending_keys.add(task_key)

    active_links = await asyncio.to_thread(store.active_jira_task_agent_links)
    for link in active_links:
        task_key = str(link["task_key"])
        if task_key not in today_keys:
            continue
        task = await snapshot(task_key)
        if str(getattr(task, "status", "")).casefold() in FINAL_STATUSES:
            continue
        await add_pending(task_key, "уже в работе; продолжу на ближайшей проверке")

    failed_keys: list[str] = []
    for run in today_runs:
        if run.get("status") in {"failed", "interrupted"}:
            failed_keys.extend(_run_task_keys(run))
    for task_key in dict.fromkeys(failed_keys):
        await add_pending(task_key, "запуск завершился ошибкой; повторю на следующем запуске")

    if reader is not None:
        try:
            queued = await reader.queued_tasks()
            for index, task in enumerate(queued, 1):
                await add_pending(
                    task.key,
                    f"№{index} в общей очереди; возьму на ближайшей часовой проверке",
                )
        except Exception:
            LOGGER.warning("Daily digest could not read the general Jira queue")

    for selector, label in ((select_design, "дизайн"), (select_analytics, "аналитика")):
        try:
            selected = await selector(settings)
        except Exception:
            LOGGER.warning("Daily digest could not select the next %s task", label)
            continue
        if selected is not None:
            await add_pending(
                selected.task_key,
                f"первая в очереди «{label}»; возьму в работу {_next_weekday_label(current)}",
            )

    usage: ClaudeUsage | None = None
    quota_note: str | None = None
    try:
        usage = await read_usage(settings.claude_cli, model=settings.claude_model)
        if not weekly_quota_allows_launch(usage, now=current):
            quota_note = (
                "Лимита для полного запуска сейчас недостаточно; продолжу очередь "
                "после восстановления лимита."
            )
    except Exception:
        quota_note = (
            "Проверка лимита или сервис ИИ недоступны; очередь сохранена, продолжу "
            "после восстановления."
        )

    return DigestFacts(tuple(completed), tuple(pending), quota_note), usage


def render_fallback(facts: DigestFacts) -> str:
    """Render a useful Slack message without any model call."""
    lines = ["*Сделано сегодня*"]
    if facts.completed:
        for item in facts.completed:
            suffix = (
                f"[{item.note}]({item.document_url})"
                if item.document_url
                else item.note
            )
            lines.append(f"- [{item.title}]({item.jira_url}) — {suffix}.")
    else:
        lines.append("- Завершённых задач сегодня нет.")
    lines.extend(("", "*Не завершено и что дальше*"))  # noqa: RUF001
    if facts.quota_note:
        lines.append(f"- {facts.quota_note}")
    if facts.pending:
        lines.extend(f"- [{item.title}]({item.jira_url}) — {item.note}." for item in facts.pending)
    elif not facts.quota_note:
        lines.append("- Застрявших задач и очереди сейчас нет.")
    return "\n".join(lines)


def is_valid_agent_digest(text: str, draft: str) -> bool:
    """Reject formatting output that loses facts/links or leaks bare URLs."""
    stripped = text.strip()
    if "Сделано сегодня" not in stripped or "Не завершено" not in stripped:  # noqa: RUF001
        return False
    plural_forms = re.compile(
        r"\b(?:мы|продолжим|сделаем|проверим|возьмём|вернёмся)\b",
        re.IGNORECASE,
    )
    if plural_forms.search(stripped):
        return False
    required_urls = set(URL_RE.findall(draft))
    if not required_urls.issubset(set(URL_RE.findall(stripped))):
        return False
    without_links = re.sub(r"\[[^]]+\]\(https://[^)]+\)", "", stripped)
    return URL_RE.search(without_links) is None


async def build_digest(
    settings: Settings,
    store: DigestStore,
    agent: AgentSubmitter,
    *,
    now: dt.datetime | None = None,
) -> tuple[str, bool]:
    """Build the digest and return whether the agent formatter was used."""
    current = now or dt.datetime.now(dt.UTC)
    facts, usage = await collect_facts(settings, store, now=current)
    draft = render_fallback(facts)
    if usage is None or not weekly_quota_allows_launch(usage, now=current):
        return draft, False
    prompt = f"{DIGEST_PROMPT}\n\nAuthoritative draft:\n{draft}"
    try:
        run = await asyncio.wait_for(
            agent.execute_once(
                prompt,
                TIMEOUT_SECONDS,
                job_name=JOB_NAME,
                workspace=PROJECT_ROOT,
                accept_result=lambda text: is_valid_agent_digest(text, draft),
                max_interim_results=0,
            ),
            timeout=TIMEOUT_SECONDS + 15,
        )
    except Exception:
        LOGGER.exception("Daily activity digest formatter failed; using deterministic fallback")
        return draft, False
    return (run.text.strip(), True) if is_valid_agent_digest(run.text, draft) else (draft, False)


async def run_once(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    store: DigestStore,
    *,
    now: dt.datetime | None = None,
) -> tuple[str, bool]:
    message, used_agent = await build_digest(settings, store, agent, now=now)
    conversation = await client.conversations_open(users=settings.slack_user_id)
    await client.chat_postMessage(
        channel=conversation["channel"]["id"],
        markdown_text=message,
        unfurl_links=False,
        unfurl_media=False,
    )
    return message, used_agent


async def run_weekdays(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    store: DigestStore,
    enabled: Callable[[], bool] = lambda: True,
) -> None:
    while True:
        now = dt.datetime.now(dt.UTC)
        target = next_run_at(now)
        LOGGER.info("Next daily activity digest scheduled for %s", target.isoformat())
        await asyncio.sleep((target.astimezone(dt.UTC) - now).total_seconds())
        if not enabled():
            LOGGER.info("Scheduled daily activity digest disabled from admin")
            continue
        try:
            LOGGER.info("Starting scheduled daily activity digest")
            await run_once(client, agent, settings, store)
            LOGGER.info("Daily activity digest completed")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Could not complete the daily activity digest")


async def cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
