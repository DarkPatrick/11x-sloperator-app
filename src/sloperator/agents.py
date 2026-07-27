"""Durable Claude Code and Codex sessions backed by Slack threads."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from sloperator.codex_app_server import CodexAppServer
from sloperator.config import Settings
from sloperator.store import AgentSession, EventStore
from sloperator.vpn import VpnManager, VpnState

LOGGER = logging.getLogger(__name__)
DIRECTIVE_RE = re.compile(
    r"^\[(?P<provider>claude|codex)(?::(?P<model>[A-Za-z0-9._:-]{1,100}))?\]\s*",
    re.IGNORECASE,
)
NEXT_RE = re.compile(r"^next:\s*", re.IGNORECASE)
ARTIFACT_RE = re.compile(r"^SLOPERATOR_ARTIFACT:\s*(?P<path>\S+)\s*$")
INITIAL_INSTRUCTION = """\
Act as a pragmatic product analyst embedded in the Ultimate Guitar monetisation team.
Before doing substantive work, run scripts/freshness_preflight.sh as required by AGENTS.md.
Work in the current ug-ai-analyst repository and follow all repository instructions.
Write for product and monetisation teammates: lead with the finding, use plain language,
state concrete human-readable facts, and finish with prioritised recommendations. Avoid
ceremonial intros such as "Investigation complete", meta-commentary about making a
Slack-ready summary, horizontal rules, jargon, and repetition.

Return a concise self-contained response using standard Markdown supported by Slack.
Slack does not render Markdown tables, so use short lists in the message. Tables and
charts are encouraged in attached reports. For a large investigation, use the repository's
dataviz helper to build a readable self-contained HTML report when useful.

If you create SQL, scripts, charts, data extracts, or an HTML report, package the useful
artifacts into one ZIP archive inside the repository. Do not merely list server paths.
End the response with exactly `SLOPERATOR_ARTIFACT: relative/path/to/archive.zip` on its
own line; the bot removes this line and attaches the archive to the Slack thread.
Do not include secrets, credentials, raw personal data, or unrelated files in the archive.

If you need clarification, ask one concise question in the final response instead of
waiting for terminal input.

User request:
"""


class AgentExecutionError(RuntimeError):
    """Raised when an agent CLI turn cannot complete successfully."""


class AgentSteeringInterrupt(RuntimeError):
    """Raised after a Claude process was interrupted for new user guidance."""


class SubmitResult(StrEnum):
    """How an incoming Slack message was routed."""

    QUEUED = "queued"
    STEERED = "steered"
    DUPLICATE = "duplicate"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """Provider selection parsed from the first message in a Slack thread."""

    provider: str
    model: str
    prompt: str


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """A resumable CLI turn result."""

    session_id: str
    text: str


class ActiveAgentRun:
    """Mutable control surface for one running provider turn."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.process: asyncio.subprocess.Process | None = None
        self.codex: CodexAppServer | None = None
        self._claude_steering: list[str] = []
        self._lock = asyncio.Lock()

    async def steer(self, text: str) -> bool:
        """Steer Codex natively or interrupt Claude for an immediate resume."""
        async with self._lock:
            if self.provider == "codex":
                return self.codex is not None and await self.codex.steer(text)
            self._claude_steering.append(text)
            if self.process is not None:
                await _terminate_process(self.process)
            return True

    def take_claude_steering(self) -> list[str]:
        """Consume guidance accumulated since the previous Claude launch."""
        messages, self._claude_steering = self._claude_steering, []
        return messages

    @property
    def has_claude_steering(self) -> bool:
        return bool(self._claude_steering)


def validate_agent_runtime(settings: Settings) -> None:
    """Fail startup when the configured workspace or CLI binaries are unavailable."""
    if not settings.agent_workspace.is_dir():
        raise ValueError(f"Agent workspace does not exist: {settings.agent_workspace}")
    if not (settings.agent_workspace / "AGENTS.md").is_file():
        raise ValueError(f"Agent workspace has no AGENTS.md: {settings.agent_workspace}")
    for name, path in (("Claude", settings.claude_cli), ("Codex", settings.codex_cli)):
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"{name} CLI is not executable: {path}")


def parse_agent_request(text: str, settings: Settings) -> AgentRequest:
    """Parse an optional ``[provider:model]`` prefix."""
    match = DIRECTIVE_RE.match(text)
    if match is None:
        provider = settings.default_agent
        model = settings.claude_model if provider == "claude" else settings.codex_model
        prompt = text.strip()
    else:
        provider = match.group("provider").lower()
        model = match.group("model") or (
            settings.claude_model if provider == "claude" else settings.codex_model
        )
        prompt = text[match.end() :].strip()
    if not prompt:
        raise ValueError("После выбора агента нужно написать запрос.")
    return AgentRequest(provider=provider, model=model, prompt=prompt)


def thread_key(message_ts: str, thread_ts: str | None) -> str:
    """Map a top-level Slack message or reply to one durable session key."""
    return thread_ts or message_ts


def _claude_steering_prompt(messages: Sequence[str]) -> str:
    guidance = "\n\n".join(messages)
    return (
        "The user sent the following additional guidance while the previous turn "
        "was running. The previous process was interrupted intentionally. Inspect "
        "the current workspace state, incorporate this guidance, and continue the task.\n\n"
        f"{guidance}"
    )


def split_slack_message(text: str, limit: int = 3_000) -> list[str]:
    """Split long agent output into Slack-safe chunks."""
    normalized = text.strip() or "Агент завершил работу без текстового ответа."
    chunks: list[str] = []
    while len(normalized) > limit:
        boundary = normalized.rfind("\n\n", 0, limit)
        if boundary < limit // 2:
            boundary = normalized.rfind("\n", 0, limit)
        if boundary < limit // 2:
            boundary = normalized.rfind(" ", 0, limit)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(normalized[:boundary].rstrip())
        normalized = normalized[boundary:].lstrip()
    chunks.append(normalized)
    return chunks


def extract_artifact(text: str, workspace: Path) -> tuple[str, Path | None]:
    """Remove and validate one agent-produced ZIP attachment marker."""
    artifact: Path | None = None
    response_lines: list[str] = []
    workspace = workspace.resolve()
    for line in text.splitlines():
        match = ARTIFACT_RE.fullmatch(line.strip())
        if match is None:
            response_lines.append(line)
            continue
        if artifact is not None:
            raise ValueError(
                "Агент указал больше одного архива с артефактами."  # noqa: RUF001
            )
        relative_path = Path(match.group("path"))
        if relative_path.is_absolute():
            raise ValueError("Путь к архиву агента должен быть относительным.")
        candidate = (workspace / relative_path).resolve()
        if not candidate.is_relative_to(workspace):
            raise ValueError("Архив агента находится за пределами рабочего каталога.")
        if candidate.suffix.lower() != ".zip" or not candidate.is_file():
            raise ValueError("Агент не создал указанный ZIP-архив.")
        artifact = candidate
    return "\n".join(response_lines).strip(), artifact


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def _run_process(
    command: Sequence[str],
    *,
    cwd: str,
    timeout_seconds: int,
    control: ActiveAgentRun | None = None,
    environment_overrides: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SLACK_", "SLOPERATOR_"))
    }
    environment["UG_SKIP_PREFLIGHT"] = "0"
    environment.update(environment_overrides or {})
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if control is not None:
        control.process = process
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        await _terminate_process(process)
        raise AgentExecutionError(
            f"Agent turn exceeded the {timeout_seconds}-second timeout"
        ) from None
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    finally:
        if control is not None and control.process is process:
            control.process = None
    if control is not None and control.has_claude_steering:
        raise AgentSteeringInterrupt
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _tail(value: str, limit: int = 4_000) -> str:
    return value[-limit:].strip()


def _with_workspace_lock(settings: Settings, command: list[str]) -> list[str]:
    """Serialize agents and automatic updates that share one working tree."""
    lock_path = settings.agent_workspace / ".git" / "sloperator-agent.lock"
    return ["/usr/bin/flock", "-x", str(lock_path), *command]


async def run_claude(
    settings: Settings,
    session: AgentSession,
    prompt: str,
    control: ActiveAgentRun,
    *,
    force_resume: bool = False,
    environment_overrides: dict[str, str] | None = None,
) -> AgentRunResult:
    """Run or resume one Claude Code turn."""
    new_session = (
        not force_resume
        and session.turn_count == 0
        and session.status not in {"failed", "cancelled"}
    )
    session_id = session.external_session_id or str(uuid.uuid4())
    command = [
        str(settings.claude_cli),
        "-p",
        "--model",
        session.model,
        "--permission-mode",
        "auto",
        "--output-format",
        "json",
    ]
    if new_session:
        command.extend(("--session-id", session_id))
        effective_prompt = f"{INITIAL_INSTRUCTION}{prompt}"
    else:
        command.extend(("--resume", session_id))
        effective_prompt = prompt
    command.append(effective_prompt)

    return_code, stdout, stderr = await _run_process(
        _with_workspace_lock(settings, command),
        cwd=str(settings.agent_workspace),
        timeout_seconds=settings.agent_timeout_seconds,
        control=control,
        environment_overrides=environment_overrides,
    )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise AgentExecutionError(
            f"Claude returned invalid JSON; stderr={_tail(stderr)!r}"
        ) from error
    if return_code != 0 or payload.get("is_error"):
        raise AgentExecutionError(
            f"Claude failed with exit code {return_code}; stderr={_tail(stderr)!r}"
        )
    result_text = payload.get("result")
    result_session_id = payload.get("session_id")
    if not isinstance(result_text, str) or not isinstance(result_session_id, str):
        raise AgentExecutionError("Claude response is missing result or session_id")
    return AgentRunResult(session_id=result_session_id, text=result_text)


async def run_codex(
    settings: Settings,
    session: AgentSession,
    prompt: str,
    control: ActiveAgentRun,
    store: EventStore,
    environment_overrides: dict[str, str] | None = None,
) -> AgentRunResult:
    """Run or resume one steerable Codex App Server turn."""
    server = CodexAppServer(
        settings.codex_cli,
        settings.agent_workspace,
        session.model,
        settings.agent_timeout_seconds,
        environment_overrides,
    )
    control.codex = server
    try:
        session_id = await server.start(session.external_session_id)
        if session.external_session_id != session_id:
            await asyncio.to_thread(
                store.set_agent_external_session_id,
                session.channel_id,
                session.thread_ts,
                session_id,
            )
        effective_prompt = (
            f"{INITIAL_INSTRUCTION}{prompt}"
            if session.external_session_id is None
            else prompt
        )
        text = await server.run_turn(effective_prompt)
        return AgentRunResult(session_id=session_id, text=text)
    except TimeoutError:
        raise AgentExecutionError(
            f"Agent turn exceeded the {settings.agent_timeout_seconds}-second timeout"
        ) from None
    finally:
        control.codex = None
        await server.close()


class AgentOrchestrator:
    """Queue agent turns and serialize messages belonging to the same thread."""

    def __init__(
        self,
        settings: Settings,
        store: EventStore,
        vpn: VpnManager | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.vpn = vpn
        self._semaphore = asyncio.Semaphore(settings.agent_max_concurrency)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._thread_tasks: dict[tuple[str, str], set[asyncio.Task[None]]] = {}
        self._active_runs: dict[tuple[str, str], ActiveAgentRun] = {}

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
    ) -> SubmitResult:
        """Deduplicate and steer an active turn or enqueue a new one."""
        claim = await asyncio.to_thread(
            self.store.prepare_agent_request,
            channel_id,
            message_ts,
            thread_ts,
        )
        if claim == SubmitResult.DUPLICATE:
            return SubmitResult.DUPLICATE
        if claim == SubmitResult.EXPIRED:
            return SubmitResult.EXPIRED
        key = (channel_id, thread_ts)
        next_match = NEXT_RE.match(text)
        if (
            next_match is None
            and (active := self._active_runs.get(key)) is not None
            and await active.steer(text)
        ):
            await asyncio.to_thread(
                self.store.finish_agent_request,
                channel_id,
                message_ts,
                SubmitResult.STEERED.value,
            )
            return SubmitResult.STEERED
        if next_match is not None:
            text = text[next_match.end() :].strip()
        task = asyncio.create_task(
            self._process(
                client,
                channel_id=channel_id,
                message_ts=message_ts,
                thread_ts=thread_ts,
                text=text,
                show_status=show_status,
                timeout_seconds=timeout_seconds,
            ),
            name=f"agent-turn-{channel_id}-{message_ts}",
        )
        self._tasks.add(task)
        self._thread_tasks.setdefault(key, set()).add(task)
        task.add_done_callback(self._task_done)
        return SubmitResult.QUEUED

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        for key, tasks in tuple(self._thread_tasks.items()):
            tasks.discard(task)
            if not tasks:
                self._thread_tasks.pop(key, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            LOGGER.error("Unhandled agent task failure: %s", type(error).__name__)

    async def cancel(self, channel_id: str, thread_ts: str) -> bool:
        """Cancel all queued or running turns belonging to one Slack thread."""
        tasks = tuple(self._thread_tasks.get((channel_id, thread_ts), ()))
        if not tasks:
            return False
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return True

    def active_keys(self) -> set[tuple[str, str]]:
        """Return Slack thread keys with queued or running in-process work."""
        return {key for key, tasks in self._thread_tasks.items() if tasks}

    async def drain(self) -> None:
        """Wait until all currently queued turns finish."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        """Cancel active CLI process groups during service shutdown."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _set_status(
        self,
        client: AsyncWebClient,
        channel_id: str,
        thread_ts: str,
        status: str,
    ) -> None:
        try:
            await client.assistant_threads_setStatus(
                channel_id=channel_id,
                thread_ts=thread_ts,
                status=status,
            )
        except SlackApiError:
            LOGGER.warning("Could not set Slack agent status for thread %s", thread_ts)

    async def _reply(
        self,
        client: AsyncWebClient,
        channel_id: str,
        thread_ts: str,
        text: str,
    ) -> None:
        response, artifact = extract_artifact(text, self.settings.agent_workspace)
        for chunk in split_slack_message(response):
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                # Agent CLIs return standard Markdown. Slack's legacy `text`
                # parameter expects its incompatible `mrkdwn` dialect, while
                # `markdown_text` lets Slack translate LLM output correctly.
                markdown_text=chunk,
            )
        if artifact is not None:
            await client.files_upload_v2(
                channel=channel_id,
                thread_ts=thread_ts,
                file=artifact,
                filename=artifact.name,
                title="Артефакты анализа",
            )

    async def _status_heartbeat(
        self,
        client: AsyncWebClient,
        channel_id: str,
        thread_ts: str,
    ) -> None:
        """Refresh Slack's two-minute status while a long agent turn runs."""
        while True:
            await asyncio.sleep(90)
            await self._set_status(client, channel_id, thread_ts, "выполняет запрос…")

    async def _process(
        self,
        client: AsyncWebClient,
        *,
        channel_id: str,
        message_ts: str,
        thread_ts: str,
        text: str,
        show_status: bool,
        timeout_seconds: int | None,
    ) -> None:
        key = (channel_id, thread_ts)
        lock = self._locks.setdefault(key, asyncio.Lock())
        request_status = "failed"
        try:
            async with lock, self._semaphore:
                session = await asyncio.to_thread(
                    self.store.get_agent_session,
                    channel_id,
                    thread_ts,
                )
                parsed = parse_agent_request(text, self.settings)
                if session is None:
                    preassigned_id = str(uuid.uuid4()) if parsed.provider == "claude" else None
                    session = await asyncio.to_thread(
                        self.store.create_agent_session,
                        channel_id,
                        thread_ts,
                        parsed.provider,
                        parsed.model,
                        preassigned_id,
                    )
                elif DIRECTIVE_RE.match(text):
                    if parsed.provider != session.provider or parsed.model != session.model:
                        await self._reply(
                            client,
                            channel_id,
                            thread_ts,
                            (
                                "Агент и модель закреплены за тредом: "
                                f"`{session.provider}:{session.model}`. "
                                "Начните новый Chat, чтобы выбрать другие параметры."
                            ),
                        )
                        request_status = "rejected"
                        return

                await asyncio.to_thread(
                    self.store.start_agent_turn,
                    channel_id,
                    thread_ts,
                )
                heartbeat: asyncio.Task[None] | None = None
                if show_status:
                    await self._set_status(
                        client,
                        channel_id,
                        thread_ts,
                        "выполняет запрос…",
                    )
                    heartbeat = asyncio.create_task(
                        self._status_heartbeat(client, channel_id, thread_ts),
                        name=f"agent-status-{channel_id}-{thread_ts}",
                    )
                control = ActiveAgentRun(session.provider)
                self._active_runs[key] = control
                environment_overrides = (
                    self.vpn.agent_environment()
                    if self.vpn is not None
                    and await self.vpn.state() is VpnState.CONNECTED
                    else None
                )
                try:
                    if session.provider == "claude":
                        prompt = parsed.prompt
                        force_resume = False
                        while True:
                            try:
                                result = await run_claude(
                                    replace(
                                        self.settings,
                                        agent_timeout_seconds=(
                                            timeout_seconds
                                            or self.settings.agent_timeout_seconds
                                        ),
                                    ),
                                    session,
                                    prompt,
                                    control,
                                    force_resume=force_resume,
                                    environment_overrides=environment_overrides,
                                )
                            except AgentSteeringInterrupt:
                                additions = control.take_claude_steering()
                                if not additions:
                                    raise
                                prompt = _claude_steering_prompt(additions)
                                session = replace(session, status="cancelled")
                                force_resume = True
                                continue
                            additions = control.take_claude_steering()
                            if not additions:
                                break
                            session = replace(
                                session,
                                external_session_id=result.session_id,
                                status="cancelled",
                            )
                            prompt = _claude_steering_prompt(additions)
                            force_resume = True
                    else:
                        result = await run_codex(
                            replace(
                                self.settings,
                                agent_timeout_seconds=(
                                    timeout_seconds
                                    or self.settings.agent_timeout_seconds
                                ),
                            ),
                            session,
                            parsed.prompt,
                            control,
                            self.store,
                            environment_overrides,
                        )
                finally:
                    if self._active_runs.get(key) is control:
                        self._active_runs.pop(key, None)
                    if heartbeat is not None:
                        heartbeat.cancel()
                        with suppress(asyncio.CancelledError):
                            await heartbeat
                await asyncio.to_thread(
                    self.store.finish_agent_turn,
                    channel_id,
                    thread_ts,
                    result.session_id,
                )
                await self._reply(client, channel_id, thread_ts, result.text)
                request_status = "completed"
        except ValueError as error:
            await self._reply(client, channel_id, thread_ts, str(error))
            request_status = "rejected"
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.cancel_agent_turn,
                channel_id,
                thread_ts,
            )
            request_status = "cancelled"
            raise
        except Exception as error:
            await asyncio.to_thread(
                self.store.fail_agent_turn,
                channel_id,
                thread_ts,
                repr(error),
            )
            LOGGER.error(
                "Agent turn failed in Slack thread %s: %s",
                thread_ts,
                type(error).__name__,
            )
            await self._reply(
                client,
                channel_id,
                thread_ts,
                "Агент не смог завершить запрос. Ошибка сохранена локально; попробуйте ещё раз.",
            )
        finally:
            if show_status:
                await self._set_status(client, channel_id, thread_ts, "")
            await asyncio.to_thread(
                self.store.finish_agent_request,
                channel_id,
                message_ts,
                request_status,
            )
            if not lock.locked():
                self._locks.pop(key, None)
