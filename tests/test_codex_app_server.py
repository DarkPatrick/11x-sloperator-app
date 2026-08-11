from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sloperator.codex_app_server import CodexAppServer


def test_command_makes_git_metadata_writable_inside_workspace_sandbox() -> None:
    workspace = Path("/srv/agent workspace")
    server = CodexAppServer(
        Path("/usr/bin/codex"),
        workspace,
        "gpt-5.6-sol",
        60,
        lock_workspace=False,
    )

    command = server._command()

    assert "sandbox_workspace_write.writable_roots=[\"/srv/agent workspace/.git\"]" in command
    assert command[-3:] == ["app-server", "--listen", "stdio://"]


def test_command_locks_the_same_git_directory() -> None:
    workspace = Path("/srv/agent")
    server = CodexAppServer(Path("/usr/bin/codex"), workspace, "model", 60)

    assert server._command()[:3] == [
        "/usr/bin/flock",
        "-x",
        "/srv/agent/.git/sloperator-agent.lock",
    ]


@pytest.mark.asyncio
async def test_initialize_can_wait_for_the_workspace_lock() -> None:
    server = CodexAppServer(Path("/usr/bin/codex"), Path("/srv/agent"), "model", 900)
    process = AsyncMock()
    process.returncode = None
    process.stdin = AsyncMock()
    process.stdout = AsyncMock()
    process.stderr = AsyncMock()
    server._request = AsyncMock(return_value={})  # type: ignore[method-assign]
    server._notify = AsyncMock()  # type: ignore[method-assign]
    server._read_stdout = AsyncMock()  # type: ignore[method-assign]
    server._read_stderr = AsyncMock(return_value="")  # type: ignore[method-assign]

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
        await server.connect()

    assert server._request.await_args.kwargs["request_timeout"] == 900
