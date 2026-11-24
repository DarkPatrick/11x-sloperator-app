import os
import time

from sloperator.scratch_space import configure_disk_tmp, sweep


def test_sweep_removes_only_stale_entries(tmp_path):
    old = time.time() - 7 * 3600
    stale_repo = tmp_path / "fug_8186"
    (stale_repo / "src").mkdir(parents=True)
    (stale_repo / "src" / "a.ts").write_text("x")
    stale_file = tmp_path / "fug_cs_exp.tsx"
    stale_file.write_text("x")
    for path in (stale_repo / "src" / "a.ts", stale_repo / "src", stale_repo, stale_file):
        os.utime(path, (old, old))

    active_repo = tmp_path / "claude-scratch"
    (active_repo / "repo").mkdir(parents=True)
    os.utime(active_repo, (old, old))  # top level old, but a file inside is fresh
    (active_repo / "repo" / "fresh.ts").write_text("x")

    assert sweep([tmp_path, tmp_path / "missing"]) == 2
    assert not stale_repo.exists()
    assert not stale_file.exists()
    assert (active_repo / "repo" / "fresh.ts").exists()


def test_configure_disk_tmp_sets_child_environment(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    path = configure_disk_tmp(tmp_path / "tmp")
    try:
        assert path.is_dir()
        assert os.environ["TMPDIR"] == str(path)
        assert os.environ["CLAUDE_CODE_TMPDIR"] == str(path)
        assert tempfile.gettempdir() == str(path)
    finally:
        tempfile.tempdir = None
