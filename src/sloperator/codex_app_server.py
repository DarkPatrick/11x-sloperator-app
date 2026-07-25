"""Minimal asynchronous client for the local Codex App Server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class CodexAppServerError(RuntimeError):
    """Raised when the local Codex App Server rejects or loses a request."""


class CodexAppServer:
    """Own one App Server process and one active Codex turn."""

    def __init__(
        self,
        executable: Path,
        workspace: Path,
        model: str,
        timeout_seconds: int,
    ) -> None:
        self.executable = executable
        self.workspace = workspace
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.process: asyncio.subprocess.Process | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[str] | None = None
        self._pending: dict[int, asyncio.Future[Mapping[str, Any]]] = {}
        self._notifications: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        self._request_id = 0
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _environment() -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("SLACK_", "SLOPERATOR_"))
        }
        environment["UG_SKIP_PREFLIGHT"] = "0"
        return environment

    async def start(self, existing_thread_id: str | None) -> str:
        """Start the server and create or resume its Codex thread."""
        lock_path = self.workspace / ".git" / "sloperator-agent.lock"
        command = [
            "/usr/bin/flock",
            "-x",
            str(lock_path),
            str(self.executable),
            "-c",
            'approval_policy="never"',
            "-c",
            'sandbox_mode="workspace-write"',
            "-c",
            "sandbox_workspace_write.network_access=true",
            "app-server",
            "--listen",
            "stdio://",
        ]
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.workspace),
            env=self._environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._reader_task = asyncio.create_task(self._read_stdout(), name="codex-app-server-read")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="codex-app-server-stderr")

        await self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "sloperator",
                    "title": "11x Sloperator",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._notify("initialized", {})
        if existing_thread_id is None:
            response = await self._request(
                "thread/start",
                {
                    "cwd": str(self.workspace),
                    "model": self.model,
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                    "ephemeral": False,
                },
            )
        else:
            response = await self._request(
                "thread/resume",
                {
                    "threadId": existing_thread_id,
                    "cwd": str(self.workspace),
                    "model": self.model,
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                },
            )
        thread = response.get("thread")
        if not isinstance(thread, Mapping) or not isinstance(thread.get("id"), str):
            raise CodexAppServerError("Codex did not return a thread ID")
        self.thread_id = thread["id"]
        return self.thread_id

    async def run_turn(self, prompt: str) -> str:
        """Run a turn while accepting concurrent calls to :meth:`steer`."""
        if self.thread_id is None:
            raise CodexAppServerError("Codex thread is not initialized")
        response = await self._request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        turn = response.get("turn")
        if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
            raise CodexAppServerError("Codex did not return a turn ID")
        self.turn_id = turn["id"]
        final_messages: list[str] = []

        async with asyncio.timeout(self.timeout_seconds):
            while True:
                notification = await self._notifications.get()
                method = notification.get("method")
                params = notification.get("params")
                if not isinstance(params, Mapping):
                    continue
                if method == "item/completed":
                    item = params.get("item")
                    if (
                        isinstance(item, Mapping)
                        and item.get("type") == "agentMessage"
                        and item.get("phase") == "final_answer"
                        and isinstance(item.get("text"), str)
                    ):
                        final_messages.append(item["text"])
                elif method == "turn/completed":
                    completed = params.get("turn")
                    if not isinstance(completed, Mapping) or completed.get("id") != self.turn_id:
                        continue
                    if completed.get("status") != "completed":
                        raise CodexAppServerError(
                            f"Codex turn ended with status {completed.get('status')!r}"
                        )
                    if not final_messages:
                        raise CodexAppServerError("Codex completed without a final answer")
                    return final_messages[-1]

    async def steer(self, text: str) -> bool:
        """Add user input to the currently active Codex turn."""
        if self.thread_id is None or self.turn_id is None:
            return False
        try:
            await self._request(
                "turn/steer",
                {
                    "threadId": self.thread_id,
                    "expectedTurnId": self.turn_id,
                    "input": [{"type": "text", "text": text}],
                },
            )
        except CodexAppServerError:
            return False
        return True

    async def close(self) -> None:
        """Stop the App Server and fail outstanding requests."""
        process = self.process
        if process is not None and process.returncode is None:
            if self.thread_id is not None and self.turn_id is not None:
                with suppress(CodexAppServerError):
                    await self._request(
                        "turn/interrupt",
                        {"threadId": self.thread_id, "turnId": self.turn_id},
                    )
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stderr_task

    async def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise CodexAppServerError("Codex App Server is not running")
        loop = asyncio.get_running_loop()
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[Mapping[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        await self._write({"id": request_id, "method": method, "params": params})
        try:
            message = await asyncio.wait_for(future, timeout=30)
        finally:
            self._pending.pop(request_id, None)
        error = message.get("error")
        if error is not None:
            raise CodexAppServerError(f"{method} failed: {error!r}")
        result = message.get("result")
        if not isinstance(result, Mapping):
            raise CodexAppServerError(f"{method} returned no result")
        return result

    async def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def _write(self, message: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise CodexAppServerError("Codex App Server stdin is unavailable")
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    LOGGER.warning("Ignoring invalid Codex App Server JSON")
                    continue
                if not isinstance(message, Mapping):
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int) and (future := self._pending.get(request_id)):
                    if not future.done():
                        future.set_result(message)
                elif isinstance(message.get("method"), str):
                    await self._notifications.put(message)
        finally:
            error = CodexAppServerError("Codex App Server output closed")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)

    async def _read_stderr(self) -> str:
        process = self.process
        if process is None or process.stderr is None:
            return ""
        data = await process.stderr.read()
        return data.decode("utf-8", errors="replace")[-4_000:]
