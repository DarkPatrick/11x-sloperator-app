"""Weekday owner digest with a deterministic no-agent fallback."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.agents import HeadlessAgentRun
from sloperator.automated_session_policy import (
    AUTOMATED_ATLASSIAN_IDENTITY,
    AUTOMATED_RESPONSE_STYLE,
    AUTOMATED_SESSION_REPOSITORY_POLICY,
)
from sloperator.claude_usage import ClaudeUsage, read_usage
from sloperator.config import Settings
from sloperator.experiment_analytics_planner import FAILURE_PREFIX as ANALYTICS_FAILURE_PREFIX
from sloperator.experiment_analytics_planner import select_from_jira as select_analytics
from sloperator.experiment_design_planner import FAILURE_PREFIX as DESIGN_FAILURE_PREFIX
from sloperator.experiment_design_planner import select_from_jira as select_design
from sloperator.experiment_finalizer import FAILURE_PREFIXES as FINALIZATION_FAILURE_PREFIXES
from sloperator.experiment_finalizer import NO_OP_PREFIX as FINALIZATION_NO_OP_PREFIX
from sloperator.experiment_finalizer import (
    SELECTION_RULES as FINALIZATION_SELECTION_RULES,
)
from sloperator.experiment_finalizer import next_run_at as next_finalization_run_at
from sloperator.jira_task_automation import (
    JiraTaskReader,
    is_reserved_experiment_task,
)

LOGGER = logging.getLogger(__name__)
TIMEZONE = "Asia/Nicosia"
HOUR = 19
TIMEOUT_SECONDS = 180
JOB_NAME = "daily-activity-digest"
FINALIZATION_FORECAST_JOB_NAME = "daily-activity-finalization-forecast"
CHANNEL_ID = "C018MJNU999"
DIGEST_SESSION_USED_LIMIT = 80
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_RE = re.compile(r"\bUMN-\d+\b")
URL_RE = re.compile(r"https://[^\s)>]+")
CONFLUENCE_RE = re.compile(r"https://alice\.mu\.se/[^\s)>]+")
AUTOMATION_FAILURES = (
    (DESIGN_FAILURE_PREFIX, "Дизайн эксперимента", "не удалось собрать дизайн эксперимента"),
    (ANALYTICS_FAILURE_PREFIX, "Аналитика эксперимента", "не удалось подготовить аналитику"),
    *(
        (prefix, "Итоги эксперимента", "не удалось подвести итоги эксперимента")
        for prefix in FINALIZATION_FAILURE_PREFIXES
    ),
)
FAILURE_REASONS = (
    (
        re.compile(r"Confluence pages?\b|project-page link", re.IGNORECASE),
        "нет однозначной ссылки на страницу проекта",
    ),
    (re.compile(r"start verification", re.IGNORECASE), "не подтвердился старт задачи в Jira"),
    (
        re.compile(r"selection changed|pairing changed", re.IGNORECASE),
        "задача в Jira изменилась во время запуска",
    ),
    (re.compile(r"Jira review update failed", re.IGNORECASE), "не удалось обновить задачу в Jira"),
)
# Top-level digest posts: the regular message and the low-quota placeholder thread root.
DIGEST_POST_MARKERS = ("Сделано сегодня", "Собираю информацию...")
WAITING_TITLE = "Жду разъяснений"
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
order, URL, time estimate, and uncertainty exactly. Preserve the supplied titled sections and use
short bullets. Every Jira URL must remain embedded in its task title, every Confluence URL in the
descriptive phrase such as "подготовлен документ", and every Slack URL in the corresponding
activity title or description; never print a bare URL or a separate link label.
Keep every person's full name exactly as written, as plain text without "@" or other markup.
Write about your own future actions only in the first person singular: "продолжу", "возьму",
"повторю". Never use "мы", "продолжим", "сделаем", "проверим", or another plural form. Do not
expose technical error details. Return only the Slack-ready message.

{AUTOMATED_RESPONSE_STYLE}
"""

FINALIZATION_FORECAST_PROMPT = f"""\
[claude:claude-opus-5-5]
This is an authorised automated forecast for tomorrow's UG monetisation experiment-finalisation
run. Apply the production selector below, but do not claim or transition a Jira task, edit Jira or
Confluence, publish results, or send Slack messages. This pass may run the standard calculator and
refresh only its normal calculation tables so that pending-trials evidence is current.

{AUTOMATED_SESSION_REPOSITORY_POLICY}

{AUTOMATED_ATLASSIAN_IDENTITY}

{AUTOMATED_RESPONSE_STYLE}

{FINALIZATION_SELECTION_RULES}

Forecast rules:
1. Evaluate immutable time gates at the supplied next scheduled run time, not merely at the
   current time. Re-check all other sources live.
2. Build and sort the complete preliminary pool by actual end timestamp and experiment id exactly
   as the production selector does.
3. Walk it oldest-first and obtain fresh pending-trials data. Treat a candidate as likely for the
   next run when every applicable row is already strictly below 5%, or when fresh evidence makes
   crossing that threshold by the supplied run time reasonably likely. Do not skip an older likely
   candidate for a newer one. This is a forecast, not a task claim.
4. Return at most one candidate. On success return exactly one line:
   `FINALIZATION_FORECAST: {{"id":"<id>","task":"UMN-<n>","title":"<Jira summary>"}}`
   If no candidate is likely, return exactly `FINALIZATION_FORECAST_NONE`.
   If authoritative selection or calculation cannot be completed, return exactly one line starting
   `FINALIZATION_FORECAST_FAILED:` followed by a concise reason. Return no audit or other text.
"""

FINALIZATION_FORECAST_PREFIX = "FINALIZATION_FORECAST: "
FINALIZATION_FORECAST_NONE = "FINALIZATION_FORECAST_NONE"
FINALIZATION_FORECAST_FAILED = "FINALIZATION_FORECAST_FAILED:"


@dataclass(frozen=True, slots=True)
class DigestItem:
    task_key: str
    title: str
    jira_url: str
    note: str
    document_url: str | None = None
    slack_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DigestFacts:
    completed: tuple[DigestItem, ...]
    pending: tuple[DigestItem, ...]
    active: tuple[DigestItem, ...] = ()
    waiting: tuple[DigestItem, ...] = ()
    continuation_title: str = "Продолжу завтра"
    quota_note: str | None = None
    queue_notes: tuple[str, ...] = ()


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

    async def submit(
        self,
        client: AsyncWebClient,
        *,
        channel_id: str,
        message_ts: str,
        thread_ts: str,
        text: str,
        show_status: bool = True,
        timeout_seconds: int | None = None,
        disable_link_previews: bool = False,
        optional_reply: bool = False,
        require_artifact: bool = False,
        automated: bool = False,
        react_to_message: bool = True,
        agent_name: str | None = None,
        wait_for_quota_admission: bool = False,
        quota_admission_session_used_limit: int = 50,
        quota_admission_check_weekly: bool = True,
    ) -> Any: ...

    async def resume_interrupted_headless(
        self,
        timeout_seconds: int,
        *,
        job_name: str | None = None,
        workspace: Path | None = None,
        accept_result: Callable[[str], bool] = lambda _: True,
        max_interim_results: int = 2,
    ) -> list[HeadlessAgentRun]: ...


class DigestStore(Protocol):
    def list_scheduled_agent_runs(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def list_interrupted_scheduled_agent_runs(
        self, job_name: str | None = None
    ) -> list[dict[str, object]]: ...

    def active_jira_task_agent_links(self) -> list[dict[str, Any]]: ...

    def list_agent_sessions(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def thread_messages(
        self, channel_id: str, thread_ts: str, limit: int = 30
    ) -> list[dict[str, Any]]: ...

    def bot_channel_messages(
        self, channel_ids: list[str], since_ts: float, limit: int = 200
    ) -> list[dict[str, Any]]: ...

    def finish_scheduled_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        external_session_id: str | None = None,
        result_text: str | None = None,
        last_error: str | None = None,
    ) -> None: ...


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
    return _timestamp_day(run.get("updated_at"), timezone)


def _timestamp_day(value: Any, timezone: ZoneInfo) -> dt.date | None:
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


def _experiment_config_ids(run: dict[str, Any]) -> tuple[str, ...]:
    if run.get("channel_name") != "experiment-config-check" or run.get("status") != "completed":
        return ()
    prompt = str(run.get("messages", [{}])[0].get("text", ""))
    selection = re.search(
        r"Experiments to review:(.*?)(?:For every experiment|\Z)",
        prompt,
        re.IGNORECASE | re.DOTALL,
    )
    if selection is None:
        return ()
    return tuple(dict.fromkeys(re.findall(r"\bid=(\d{3,})\b", selection.group(1))))


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


def _continuation_title(now: dt.datetime) -> str:
    local_now = now.astimezone(ZoneInfo(TIMEZONE))
    if local_now.weekday() == 4:
        return "Продолжу на следующей неделе"
    return "Продолжу завтра"


def _general_queue(tasks: list[Any]) -> list[Any]:
    """Keep ordinary ug-ai-analyst Jira work; specialized crons own reserved tasks."""
    return [task for task in tasks if not is_reserved_experiment_task(str(task.summary))]


def digest_quota_allows_launch(usage: ClaudeUsage) -> bool:
    """Use only the five-hour allowance for this small digest formatting pass."""
    return usage.session_used_percent < DIGEST_SESSION_USED_LIMIT


def _finalization_forecast_marker(text: str) -> str | None:
    markers = [
        line.strip()
        for line in text.splitlines()
        if line.strip() == FINALIZATION_FORECAST_NONE
        or line.strip().startswith(
            (FINALIZATION_FORECAST_PREFIX, FINALIZATION_FORECAST_FAILED)
        )
    ]
    return markers[0] if len(markers) == 1 else None


def _parse_finalization_forecast(text: str, settings: Settings) -> DigestItem | None:
    stripped = _finalization_forecast_marker(text)
    if stripped is None:
        raise ValueError("Finalization forecast returned no unambiguous marker")
    if stripped == FINALIZATION_FORECAST_NONE:
        return None
    if stripped.startswith(FINALIZATION_FORECAST_FAILED):
        raise ValueError(stripped)
    if not stripped.startswith(FINALIZATION_FORECAST_PREFIX):
        raise ValueError("Finalization forecast returned an invalid marker")
    try:
        payload = json.loads(stripped.removeprefix(FINALIZATION_FORECAST_PREFIX))
    except json.JSONDecodeError as error:
        raise ValueError("Finalization forecast returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("Finalization forecast payload is not an object")
    experiment_id = str(payload.get("id", "")).strip()
    task_key = str(payload.get("task", "")).strip().upper()
    task_title = " ".join(str(payload.get("title", "")).split())
    if not experiment_id.isdigit() or TASK_RE.fullmatch(task_key) is None or not task_title:
        raise ValueError("Finalization forecast payload is incomplete")
    return DigestItem(
        task_key,
        task_title,
        f"{settings.jira_url.rstrip('/')}/browse/{task_key}",
        f"итоги эксперимента {experiment_id}; по текущей оценке возьму в работу завтра",
    )


def _is_finalization_forecast(text: str) -> bool:
    stripped = _finalization_forecast_marker(text)
    if stripped is None:
        return False
    if stripped == FINALIZATION_FORECAST_NONE or stripped.startswith(
        FINALIZATION_FORECAST_FAILED
    ):
        return True
    if not stripped.startswith(FINALIZATION_FORECAST_PREFIX):
        return False
    try:
        payload = json.loads(stripped.removeprefix(FINALIZATION_FORECAST_PREFIX))
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and str(payload.get("id", "")).isdigit()
        and TASK_RE.fullmatch(str(payload.get("task", "")).upper()) is not None
        and bool(str(payload.get("title", "")).strip())
    )


async def forecast_finalization(
    settings: Settings,
    agent: AgentSubmitter,
    *,
    now: dt.datetime,
) -> DigestItem | None:
    """Forecast the one Results task most likely to pass tomorrow's production selector."""
    target = next_finalization_run_at(
        now,
        settings.experiment_finalizer_timezone,
        settings.experiment_finalizer_hour,
    )
    run = await agent.execute_once(
        FINALIZATION_FORECAST_PROMPT
        + "\n\nAuthoritative next scheduled run time: "
        + target.isoformat(),
        settings.experiment_finalizer_timeout_seconds,
        job_name=FINALIZATION_FORECAST_JOB_NAME,
        workspace=settings.agent_workspace,
        accept_result=_is_finalization_forecast,
        max_interim_results=0,
    )
    return _parse_finalization_forecast(run.text, settings)


async def _add_finalization_forecast(
    facts: DigestFacts,
    settings: Settings,
    agent: AgentSubmitter,
    *,
    now: dt.datetime,
) -> DigestFacts:
    try:
        item = await forecast_finalization(settings, agent, now=now)
    except Exception:
        LOGGER.exception("Daily digest could not forecast the next finalization task")
        return replace(
            facts,
            queue_notes=(
                *facts.queue_notes,
                "Очередь итогов не удалось надёжно проверить; повторю проверку по расписанию.",
            ),
        )
    return _add_finalization_forecast_item(facts, item, now=now)


def _add_finalization_forecast_item(
    facts: DigestFacts,
    item: DigestItem | None,
    *,
    now: dt.datetime,
) -> DigestFacts:
    if item is None:
        return replace(
            facts,
            queue_notes=(
                *facts.queue_notes,
                "По текущей оценке завтра нет задачи по итогам, проходящей все фильтры.",
            ),
        )
    experiment_id_match = re.search(r"\d+", item.note)
    if experiment_id_match is None:  # pragma: no cover - guarded by forecast parsing
        raise ValueError("Finalization forecast item has no experiment id")
    item = replace(
        item,
        note=(
            f"итоги эксперимента {experiment_id_match.group()}; "
            f"по текущей оценке возьму в работу {_next_weekday_label(now)}"
        ),
    )
    if item.task_key in {entry.task_key for entry in (*facts.completed, *facts.pending)}:
        return facts
    return replace(facts, pending=(*facts.pending, item))


def _add_recovered_finalization_forecast(
    facts: DigestFacts,
    settings: Settings,
    text: str,
    *,
    now: dt.datetime,
) -> DigestFacts:
    """Apply a recovered forecast result without launching a replacement session."""
    try:
        item = _parse_finalization_forecast(text, settings)
    except Exception:
        LOGGER.exception("Daily digest could not use the recovered finalization forecast")
        return replace(
            facts,
            queue_notes=(
                *facts.queue_notes,
                "Очередь итогов не удалось надёжно проверить; повторю проверку по расписанию.",
            ),
        )
    return _add_finalization_forecast_item(facts, item, now=now)


def _slack_thread_url(channel_id: str, thread_ts: str) -> str:
    anchor = thread_ts.replace(".", "")
    return (
        f"https://mu--se.slack.com/archives/{channel_id}/p{anchor}"
        f"?thread_ts={thread_ts}&cid={channel_id}"
    )


def _activity_refs(text: str) -> set[str]:
    """Return stable work identifiers shared by preparer, reviewer and Slack publication."""
    refs = {f"jira:{key}" for key in TASK_RE.findall(text)}
    refs.update(
        f"confluence:{match}"
        for match in re.findall(r"(?:pageId=|/pages/)(\d{5,})", text)
    )
    refs.update(
        f"experiment:{match}"
        for match in re.findall(r"/experiment/view\?id=(\d+)", text)
    )
    return refs


def _item_refs(item: DigestItem) -> set[str]:
    refs: set[str] = set()
    if TASK_RE.fullmatch(item.task_key):
        refs.add(f"jira:{item.task_key}")
    refs.update(
        _activity_refs(
            "\n".join(
                filter(None, (item.title, item.note, item.jira_url, item.document_url))
            )
        )
    )
    return refs


def _merge_completed_session(
    completed: list[DigestItem],
    session_item: DigestItem,
    session_refs: set[str],
) -> bool:
    """Attach a Slack publication to the same completed work instead of duplicating it."""
    if not session_refs:
        return False
    for index, item in enumerate(completed):
        if not session_refs.intersection(_item_refs(item)):
            continue
        completed[index] = replace(
            item,
            slack_urls=tuple(dict.fromkeys((*item.slack_urls, session_item.jira_url))),
        )
        return True
    return False


def _is_no_op_finalizer_thread(
    session: dict[str, Any], messages: list[dict[str, Any]]
) -> bool:
    """A finalizer thread that only says no experiment qualified is not work done."""
    if session.get("agent_name") != "experiment-finalizer/slack":
        return False
    texts = [str(message.get("text", "")).strip() for message in messages]
    texts = [text for text in texts if text]
    return bool(texts) and all(text.startswith(FINALIZATION_NO_OP_PREFIX) for text in texts)


def _slack_session_item(session: dict[str, Any]) -> tuple[str, DigestItem] | None:
    """Turn one substantive Slack agent session into a completed or pending item."""
    status = str(session.get("status", ""))
    turns = int(session.get("turn_count") or 0)
    if status == "idle" and turns < 1:
        return None
    channel_id = str(session["channel_id"])
    channel_name = str(session.get("channel_name") or channel_id)
    thread_ts = str(session["thread_ts"])
    agent_name = str(session.get("agent_name") or "")
    url = _slack_thread_url(channel_id, thread_ts)

    if agent_name == "mobile-health/slack":
        title = "Разбор аномалий мобильной монетизации"
        completed_note = (
            "выполнил разбор и дополнил его по запросу"  # noqa: RUF001
            if turns > 1
            else "выполнил разбор"
        )
    elif agent_name == "web-health/slack":
        title = "Разбор аномалий веб-монетизации"
        completed_note = (
            "проверил алерт и связал его с готовым разбором"  # noqa: RUF001
        )
    elif agent_name == "anomaly-alerts/slack":
        title = f"Разбор аномалий в #{channel_name}"
        completed_note = "выполнил разбор"
    elif agent_name == "experiment-finalizer/slack":
        title = "Обновление страницы с итогами эксперимента"  # noqa: RUF001
        completed_note = "обновил страницу с итогами"  # noqa: RUF001
    elif agent_name == "experiment-config-check/slack":
        title = "Проверка конфигурации эксперимента"
        completed_note = "проверил конфигурацию"
    else:
        title = f"Работа агента в #{channel_name}"
        completed_note = "завершил работу"

    if status == "idle":
        bucket, note = "completed", completed_note
    elif status in {"running", "queued"}:
        bucket, note = "active", "сейчас выполняю разбор"
    elif status == "failed":
        bucket, note = "pending", "запуск завершился ошибкой; повторю позднее"
    else:
        bucket, note = "pending", "запуск не завершён; повторю позднее"
    return bucket, DigestItem(
        f"slack:{channel_id}:{thread_ts}",
        title,
        url,
        note,
    )


@dataclass(frozen=True, slots=True)
class AutomationFailure:
    task_key: str | None
    title: str
    note: str


def parse_automation_failure(
    channel_id: str, message_ts: str, text: str
) -> AutomationFailure | None:
    """Turn a published experiment-automation failure into a plain digest outcome."""
    stripped = text.strip()
    for prefix, title, outcome in AUTOMATION_FAILURES:
        if not stripped.startswith(prefix):
            continue
        detail = stripped.removeprefix(prefix).strip()
        owned = re.match(r"(UMN-\d+):", detail) or re.search(r"\bResults task (UMN-\d+)", detail)
        reason = next(
            (plain for pattern, plain in FAILURE_REASONS if pattern.search(detail)), None
        )
        task_key = owned.group(1) if owned else None
        note = f"{outcome}: {reason}" if reason else outcome
        if reason is None or task_key is None:
            note += f" ([подробности в Slack]({_slack_thread_url(channel_id, message_ts)}))"
        return AutomationFailure(task_key, title, note)
    return None


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
    config_ids = tuple(
        dict.fromkeys(
            experiment_id
            for run in reversed(today_runs)
            for experiment_id in _experiment_config_ids(run)
        )
    )
    if config_ids:
        config_links = ", ".join(
            "[{}]({}/components/ab/experiment/view?id={})".format(
                experiment_id,
                "https://www.ultimate-guitar.com",
                experiment_id,
            )
            for experiment_id in config_ids
        )
        completed.append(
            DigestItem(
                f"experiment-config:{today.isoformat()}",
                "Проверка конфигураций экспериментов",
                "",
                f"проверил ID экспериментов: {config_links}",
            )
        )
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

    day_start = dt.datetime.combine(today, dt.time(), timezone).timestamp()
    failure_channels = list(
        dict.fromkeys(
            (
                settings.experiment_design_channel,
                settings.experiment_analytics_channel,
                settings.experiment_finalizer_channel,
            )
        )
    )
    try:
        failure_messages = await asyncio.to_thread(
            store.bot_channel_messages, failure_channels, day_start
        )
    except Exception:
        LOGGER.warning("Daily digest could not read published automation failures")
        failure_messages = []
    for message in reversed(failure_messages):
        failure = parse_automation_failure(
            str(message["channel_id"]), str(message["message_ts"]), str(message["text"])
        )
        if failure is None:
            continue
        item_key = failure.task_key or f"slack:{message['channel_id']}:{message['message_ts']}"
        if item_key in completed_keys:
            continue
        task = await snapshot(failure.task_key) if failure.task_key else None
        completed.append(
            DigestItem(
                item_key,
                _task_title(task, failure.task_key) if failure.task_key else failure.title,
                (
                    f"{settings.jira_url.rstrip('/')}/browse/{failure.task_key}"
                    if failure.task_key
                    else ""
                ),
                failure.note,
            )
        )
        completed_keys.add(item_key)

    try:
        sessions = await asyncio.to_thread(store.list_agent_sessions, 500)
    except Exception:
        LOGGER.warning("Daily digest could not read Slack agent sessions")
        sessions = []
    today_sessions = [
        session
        for session in sessions
        if today
        in {
            _timestamp_day(session.get("created_at"), timezone),
            _timestamp_day(session.get("updated_at"), timezone),
        }
    ]
    active: list[DigestItem] = []
    slack_pending: list[DigestItem] = []
    for session in reversed(today_sessions):
        shaped = _slack_session_item(session)
        if shaped is None:
            continue
        bucket, item = shaped
        if bucket == "completed":
            try:
                messages = await asyncio.to_thread(
                    store.thread_messages,
                    str(session["channel_id"]),
                    str(session["thread_ts"]),
                    100,
                )
            except Exception:
                LOGGER.warning("Daily digest could not read one Slack agent thread")
                messages = []
            if _is_no_op_finalizer_thread(session, messages):
                continue
            session_refs = _activity_refs(
                "\n".join(str(message.get("text", "")) for message in messages)
            )
            if _merge_completed_session(completed, item, session_refs):
                continue
            completed.append(item)
            completed_keys.add(item.task_key)
        elif bucket == "active":
            active.append(item)
        else:
            slack_pending.append(item)

    pending: list[DigestItem] = []
    pending_keys: set[str] = set()
    queue_notes: list[str] = []
    today_keys = {task_key for run in today_runs for task_key in _run_task_keys(run)}

    async def add_pending(task_key: str, note: str) -> None:
        if task_key in completed_keys or task_key in pending_keys or task_key in waiting_keys:
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

    async def add_active(task_key: str, note: str) -> None:
        if (
            task_key in completed_keys
            or task_key in waiting_keys
            or any(item.task_key == task_key for item in active)
        ):
            return
        task = await snapshot(task_key)
        active.append(
            DigestItem(
                task_key,
                _task_title(task, task_key),
                f"{settings.jira_url.rstrip('/')}/browse/{task_key}",
                note,
            )
        )

    pending.extend(slack_pending)
    pending_keys.update(item.task_key for item in slack_pending)

    active_links = await asyncio.to_thread(store.active_jira_task_agent_links)
    waiting: list[DigestItem] = []
    waiting_keys: set[str] = set()
    if reader is not None:
        for link in active_links:
            task_key = str(link["task_key"])
            if task_key in completed_keys or task_key in waiting_keys:
                continue
            task = await snapshot(task_key)
            if task is None or str(getattr(task, "status", "")).casefold() in FINAL_STATUSES:
                continue
            try:
                request = await reader.clarification_request(task_key)
            except Exception:
                LOGGER.warning("Daily digest could not read Jira comments of %s", task_key)
                continue
            if request is None:
                continue
            waiting.append(
                DigestItem(
                    task_key,
                    _task_title(task, task_key),
                    f"{settings.jira_url.rstrip('/')}/browse/{task_key}",
                    (
                        f"задал вопрос в задаче, жду ответа от {request.requested_from}"
                        if request.requested_from
                        else "задал вопрос в задаче, жду ответа автора"
                    ),
                )
            )
            waiting_keys.add(task_key)

    for link in active_links:
        task_key = str(link["task_key"])
        if task_key not in today_keys:
            continue
        task = await snapshot(task_key)
        if str(getattr(task, "status", "")).casefold() in FINAL_STATUSES:
            continue
        await add_active(task_key, "уже в работе; завершаю")

    failed_keys: list[str] = []
    for run in today_runs:
        if run.get("status") in {"failed", "interrupted"}:
            failed_keys.extend(_run_task_keys(run))
    for task_key in dict.fromkeys(failed_keys):
        await add_pending(task_key, "запуск завершился ошибкой; повторю на следующем запуске")

    if reader is not None:
        try:
            queued = _general_queue(await reader.queued_tasks())
            for index, task in enumerate(queued, 1):
                await add_pending(
                    task.key,
                    f"№{index} в общей очереди; возьму на ближайшей часовой проверке",
                )
        except Exception:
            LOGGER.warning("Daily digest could not read the general Jira queue")
            queue_notes.append(
                "Общую Jira-очередь не удалось надёжно проверить; повторю проверку по расписанию."
            )

    for selector, label in ((select_design, "дизайн"), (select_analytics, "аналитика")):
        try:
            selected = await selector(settings)
        except Exception:
            LOGGER.warning("Daily digest could not select the next %s task", label)
            queue_notes.append(
                f"Очередь «{label}» не удалось надёжно проверить; повторю проверку по расписанию."
            )
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
        if not digest_quota_allows_launch(usage):
            quota_note = (
                "Лимит для новых агентских запусков ниже безопасного порога; проверю "
                "очередь снова по обычному расписанию. Этот дайджест автоматически "
                "не обновляется."
            )
    except Exception:
        quota_note = (
            "Не смог проверить лимит или доступность ИИ; новые запуски останутся на "  # noqa: RUF001
            "плановых повторах. Этот дайджест автоматически не обновляется."
        )

    return DigestFacts(
        tuple(completed),
        tuple(pending),
        active=tuple(active),
        waiting=tuple(waiting),
        continuation_title=_continuation_title(current),
        quota_note=quota_note,
        queue_notes=tuple(queue_notes),
    ), usage


def render_fallback(facts: DigestFacts) -> str:
    """Render a useful Slack message without any model call."""
    lines = ["*Сделано сегодня*"]
    if facts.completed:
        for item in facts.completed:
            suffix = item.note
            if item.document_url:
                suffix = f"[{item.note}]({item.document_url})"
            if item.slack_urls:
                slack_links = ", ".join(
                    f"[результат в Slack{f' {index}' if len(item.slack_urls) > 1 else ''}]({url})"
                    for index, url in enumerate(item.slack_urls, 1)
                )
                suffix = f"{suffix}, {slack_links}"
            title = f"[{item.title}]({item.jira_url})" if item.jira_url else item.title
            lines.append(f"- {title} — {suffix}.")
    else:
        lines.append("- Завершённых задач сегодня нет.")
    if facts.active:
        lines.extend(("", "*Завершаю работу*"))
        for item in facts.active:
            title = f"[{item.title}]({item.jira_url})" if item.jira_url else item.title
            lines.append(f"- {title} — {item.note}.")
    lines.extend(("", f"*{facts.continuation_title}*"))
    if facts.quota_note:
        lines.append(f"- {facts.quota_note}")
    for note in facts.queue_notes:
        lines.append(f"- {note}")
    if facts.pending:
        for item in facts.pending:
            title = f"[{item.title}]({item.jira_url})" if item.jira_url else item.title
            lines.append(f"- {title} — {item.note}.")
    elif not facts.quota_note and not facts.queue_notes:
        lines.append("- Застрявших задач и очереди сейчас нет.")
    if facts.waiting:
        lines.extend(("", f"*{WAITING_TITLE}*"))
        for item in facts.waiting:
            title = f"[{item.title}]({item.jira_url})" if item.jira_url else item.title
            lines.append(f"- {title} — {item.note}.")
    return "\n".join(lines)


def is_valid_agent_digest(text: str, draft: str) -> bool:
    """Reject formatting output that loses facts/links or leaks bare URLs."""
    stripped = text.strip()
    if "Сделано сегодня" not in stripped or not re.search(
        r"\*?Продолжу (?:завтра|на следующей неделе)\*?", stripped
    ):
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
    if not set(TASK_RE.findall(draft)).issubset(set(TASK_RE.findall(stripped))):
        return False
    forecast_ids = set(re.findall(r"итоги эксперимента (\d+)", draft, re.IGNORECASE))
    if not forecast_ids.issubset(
        set(re.findall(r"итоги эксперимента (\d+)", stripped, re.IGNORECASE))
    ):
        return False
    if "не удалось надёжно проверить" in draft and "не удалось" not in stripped:
        return False
    if WAITING_TITLE in draft and WAITING_TITLE not in stripped:
        return False
    if not all(name in stripped for name in re.findall(r"жду ответа от ([^.\n]+)", draft)):
        return False
    if "нет задачи по итогам, проходящей все фильтры" in draft and not all(
        phrase in stripped for phrase in ("нет", "задач", "итог")
    ):
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
    if usage is None or not digest_quota_allows_launch(usage):
        facts = replace(
            facts,
            queue_notes=(
                *facts.queue_notes,
                "Очередь итогов не проверена из-за лимита запуска; "
                "повторю проверку по расписанию.",
            ),
        )
        return render_fallback(facts), False
    facts = await _add_finalization_forecast(facts, settings, agent, now=current)
    draft = render_fallback(facts)
    return await _format_draft(settings, agent, draft)


async def _format_draft(
    settings: Settings,
    agent: AgentSubmitter,
    draft: str,
) -> tuple[str, bool]:
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
    recovered_forecast: str | None = None,
) -> tuple[str, bool]:
    current = now or dt.datetime.now(dt.UTC)
    facts, usage = await collect_facts(settings, store, now=current)
    channel_id = CHANNEL_ID
    if recovered_forecast is None and (
        usage is None or not digest_quota_allows_launch(usage)
    ):
        placeholder = "Собираю информацию..."
        response = await client.chat_postMessage(channel=channel_id, text=placeholder)
        thread_ts = str(response["ts"])
        draft = render_fallback(
            replace(
                facts,
                quota_note=None,
                queue_notes=(
                    *facts.queue_notes,
                    "Очередь итогов не проверена из-за лимита запуска; "
                    "повторю проверку по расписанию.",
                ),
            )
        )
        prompt = f"{DIGEST_PROMPT}\n\nAuthoritative draft:\n{draft}"
        await agent.submit(
            client,
            channel_id=channel_id,
            message_ts=thread_ts,
            thread_ts=thread_ts,
            text=prompt,
            show_status=False,
            timeout_seconds=TIMEOUT_SECONDS,
            disable_link_previews=True,
            automated=True,
            react_to_message=False,
            agent_name="daily-activity-digest/slack",
            wait_for_quota_admission=True,
            quota_admission_session_used_limit=DIGEST_SESSION_USED_LIMIT,
            quota_admission_check_weekly=False,
        )
        return placeholder, False
    if recovered_forecast is None:
        facts = await _add_finalization_forecast(facts, settings, agent, now=current)
    else:
        facts = _add_recovered_finalization_forecast(
            facts,
            settings,
            recovered_forecast,
            now=current,
        )
    message, used_agent = await _format_draft(settings, agent, render_fallback(facts))
    await client.chat_postMessage(
        channel=channel_id,
        markdown_text=message,
        unfurl_links=False,
        unfurl_media=False,
    )
    return message, used_agent


def _created_at(value: object) -> dt.datetime | None:
    if not value:
        return None
    with suppress(ValueError, TypeError):
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
    return None


async def discard_stale_runs(store: DigestStore, *, now: dt.datetime | None = None) -> int:
    """Abandon interrupted digest stages whose day has passed or whose digest is already out.

    A digest describes one working day. Resuming it after midnight, or after the deterministic
    fallback was already published for the same run, would post a duplicate for the wrong day.
    """
    current = now or dt.datetime.now(dt.UTC)
    timezone = ZoneInfo(TIMEZONE)
    rows = [
        row
        for job_name in (JOB_NAME, FINALIZATION_FORECAST_JOB_NAME)
        for row in await asyncio.to_thread(store.list_interrupted_scheduled_agent_runs, job_name)
    ]
    created = {str(row["run_id"]): _created_at(row.get("created_at")) for row in rows}
    known = [value for value in created.values() if value is not None]
    posts: list[dict[str, Any]] = []
    if known:
        try:
            posts = await asyncio.to_thread(
                store.bot_channel_messages, [CHANNEL_ID], min(known).timestamp()
            )
        except Exception:
            LOGGER.warning("Daily digest could not read its published posts")
    post_times = [
        float(post["message_ts"])
        for post in posts
        if any(marker in str(post.get("text", "")) for marker in DIGEST_POST_MARKERS)
    ]
    discarded = 0
    for row in rows:
        run_id = str(row["run_id"])
        started = created[run_id]
        if started is None:
            continue
        if started.astimezone(timezone).date() != current.astimezone(timezone).date():
            reason = "stale digest run from a previous day; not resumed"
        elif any(posted >= started.timestamp() for posted in post_times):
            reason = "digest already published for this run; not resumed"
        else:
            continue
        await asyncio.to_thread(
            store.finish_scheduled_agent_run,
            run_id,
            status="abandoned",
            external_session_id=(
                str(row["external_session_id"]) if row.get("external_session_id") else None
            ),
            result_text=str(row["result_text"]) if row.get("result_text") is not None else None,
            last_error=reason,
        )
        LOGGER.warning("Abandoned interrupted %s run %s: %s", row.get("job_name"), run_id, reason)
        discarded += 1
    return discarded


async def resume_interrupted(
    client: AsyncWebClient,
    agent: AgentSubmitter,
    settings: Settings,
    store: DigestStore,
    *,
    now: dt.datetime | None = None,
) -> int:
    """Resume the interrupted agent stage and finish the digest workflow."""
    await discard_stale_runs(store, now=now)
    recovered_digests = await agent.resume_interrupted_headless(
        TIMEOUT_SECONDS,
        job_name=JOB_NAME,
        workspace=PROJECT_ROOT,
        accept_result=lambda text: is_valid_agent_digest(text, text),
    )
    published = 0
    for run in recovered_digests:
        message = run.text.strip()
        if not is_valid_agent_digest(message, message):
            LOGGER.error("Recovered daily activity digest did not pass output validation")
            if run.run_id is not None:
                await asyncio.to_thread(
                    store.finish_scheduled_agent_run,
                    run.run_id,
                    status="failed",
                    external_session_id=run.session_id,
                    result_text=run.text,
                    last_error="recovered digest failed output validation",
                )
            continue
        await client.chat_postMessage(
            channel=CHANNEL_ID,
            markdown_text=message,
            unfurl_links=False,
            unfurl_media=False,
        )
        if run.run_id is not None:
            await asyncio.to_thread(
                store.finish_scheduled_agent_run,
                run.run_id,
                status="completed",
                external_session_id=run.session_id,
                result_text=run.text,
            )
        published += 1
    if recovered_digests:
        # A restart can interrupt the formatter after its recovered forecast handed off.
        # Close that upstream recovered run once the formatter has actually published.
        if published:
            upstream_forecasts = await asyncio.to_thread(
                store.list_interrupted_scheduled_agent_runs,
                FINALIZATION_FORECAST_JOB_NAME,
            )
            for upstream in upstream_forecasts:
                if upstream.get("status") != "recovered":
                    continue
                await asyncio.to_thread(
                    store.finish_scheduled_agent_run,
                    str(upstream["run_id"]),
                    status="completed",
                    external_session_id=(
                        str(upstream["external_session_id"])
                        if upstream.get("external_session_id") is not None
                        else None
                    ),
                    result_text=(
                        str(upstream["result_text"])
                        if upstream.get("result_text") is not None
                        else None
                    ),
                )
        return published

    recovered_forecasts = await agent.resume_interrupted_headless(
        settings.experiment_finalizer_timeout_seconds,
        job_name=FINALIZATION_FORECAST_JOB_NAME,
        workspace=settings.agent_workspace,
        accept_result=_is_finalization_forecast,
        max_interim_results=0,
    )
    for run in recovered_forecasts:
        try:
            await run_once(
                client,
                agent,
                settings,
                store,
                recovered_forecast=run.text,
            )
        except Exception as error:
            LOGGER.exception("Recovered daily activity forecast could not finish the digest")
            if run.run_id is not None:
                await asyncio.to_thread(
                    store.finish_scheduled_agent_run,
                    run.run_id,
                    status="interrupted",
                    external_session_id=run.session_id,
                    result_text=run.text,
                    last_error=repr(error),
                )
            continue
        if run.run_id is not None:
            await asyncio.to_thread(
                store.finish_scheduled_agent_run,
                run.run_id,
                status="completed",
                external_session_id=run.session_id,
                result_text=run.text,
            )
        published += 1
    return published


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
