from pathlib import Path

from sloperator.workspace_lock import is_lock_failure, with_workspace_lock, workspace_lock_path


def test_lock_lives_in_the_data_directory_not_in_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "read-only-checkout"
    workspace.mkdir()
    locks = tmp_path / "data" / "locks"

    lock = workspace_lock_path(workspace, locks)

    assert lock.parent == locks
    assert locks.is_dir()
    assert workspace not in lock.parents


def test_each_workspace_gets_its_own_stable_lock(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    first, second = tmp_path / "a" / "repo", tmp_path / "b" / "repo"

    assert workspace_lock_path(first, locks) == workspace_lock_path(first, locks)
    assert workspace_lock_path(first, locks) != workspace_lock_path(second, locks)


def test_command_is_wrapped_in_an_exclusive_flock(tmp_path: Path) -> None:
    command = with_workspace_lock(tmp_path, ["claude", "-p"], tmp_path / "locks")

    assert command[:2] == ["/usr/bin/flock", "-x"]
    assert command[-2:] == ["claude", "-p"]


def test_flock_errors_are_recognized() -> None:
    assert is_lock_failure(
        "flock: cannot open lock file /srv/repo/.git/sloperator-agent.lock: Read-only file system"
    )
    assert not is_lock_failure("Error: overloaded")
