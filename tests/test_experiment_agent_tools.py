from __future__ import annotations

import json
import sys

# ruff: noqa: ASYNC240
from pathlib import Path

import pytest

from sloperator.experiment_agent_tools import _issue_brief, excerpt, gather, wait_for


def test_issue_brief_excludes_large_description() -> None:
    issue = {
        "key": "UMN-123",
        "fields": {
            "summary": "Experiment",
            "status": {"name": "In Progress"},
            "description": "large document" * 10000,
        },
    }
    brief = _issue_brief(issue)
    assert brief["status"] == "In Progress"
    assert "description" not in brief


@pytest.mark.asyncio
async def test_gather_rejects_unbounded_targets() -> None:
    with pytest.raises(ValueError, match="Confluence page ID"):
        await gather(["UMN-123"], "https://example.com", 1)
    with pytest.raises(ValueError, match="Jira key"):
        await gather(["UMN-123", "bad"], "12345", 1)


@pytest.mark.asyncio
async def test_gather_returns_brief_context_from_exact_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sloperator.experiment_agent_tools as module

    monkeypatch.setattr(module, "WORKSPACE", tmp_path)
    (tmp_path / "output").mkdir()
    calls: list[list[str]] = []

    async def fake_capture(command: list[str], _timeout: int) -> str:
        calls.append(command)
        if command[1].endswith("jira_issue.py"):
            return json.dumps({
                "key": command[3],
                "fields": {"summary": "Test", "status": {"name": "In Progress"},
                           "description": "huge" * 10000},
            })
        artifact = Path(command[-1])
        page_text = artifact / "confluence_123.txt"
        page_text.write_text("large page body", encoding="utf-8")
        return json.dumps({
            "page_id": "123", "title": "Project", "version": 4,
            "text_path": str(page_text), "storage_path": str(artifact / "page.xhtml"),
            "json_path": str(artifact / "page.json"),
        })

    monkeypatch.setattr(module, "_capture", fake_capture)
    result = await gather(["UMN-123", "UMN-124"], "123", 5)
    assert len(calls) == 3
    assert [issue["key"] for issue in result["issues"]] == ["UMN-123", "UMN-124"]
    assert result["page"]["id"] == "123"
    assert "huge" not in json.dumps(result)


@pytest.mark.asyncio
async def test_wait_for_returns_bounded_result_and_logs() -> None:
    result = await wait_for(
        [sys.executable, "-c", "print('x' * 4000)"], 5
    )
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert len(result["stdout_tail"]) == 3000
    assert "x" * 4000 in Path(result["stdout_path"]).read_text(encoding="utf-8")


def test_excerpt_stays_in_output_and_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sloperator.experiment_agent_tools as module

    monkeypatch.setattr(module, "WORKSPACE", tmp_path)
    (tmp_path / "output").mkdir()
    page = tmp_path / "output" / "page.txt"
    page.write_text("first\n" + "A" * 10000 + " matching heading\nlast", encoding="utf-8")
    result = excerpt(str(page), "matching heading")
    assert result["matches"] == 1
    assert len(result["windows"][0]["text"]) <= 4000
    with pytest.raises(ValueError, match="saved text artifact"):
        excerpt(str(tmp_path / "outside.txt"), "heading")
