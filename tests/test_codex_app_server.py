from pathlib import Path

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
