from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.agents import HeadlessAgentRun
from sloperator.config import Settings
from sloperator.project_page_people import (
    ConfluenceHeaderReader,
    HeaderPerson,
    add_mentions,
    ensure_header_mentions,
    header_people,
    header_userkeys,
    local_display_names,
    missing_mentions,
)

HEADER = (
    '<table class="wrapped"><tbody>'
    "<tr><th>Link to OKR</th><td><p><br /></p></td></tr>"
    '<tr><th>Status</th><td><ri:user ri:userkey="status-noise" /></td></tr>'
    "<tr><th>DRI / Project owner</th><td><div class=\"content-wrapper\"><p>"
    '<ac:link><ri:user ri:userkey="key-dri" /></ac:link> </p></div></td></tr>'
    '<tr><th><div class="content-wrapper"><p>Team</p></div></th><td><p>'
    '<ac:link><ri:user ri:userkey="key-team-1" /></ac:link> '
    '<ac:link><ri:user ri:userkey="key-team-2" /></ac:link> '
    '<ac:link><ri:user ri:userkey="key-dri" /></ac:link></p></td></tr>'
    "</tbody></table>"
    '<p>Body text <ri:user ri:userkey="body-noise" /></p>'
)

HEADING = (
    "[UG App: push budget](https://alice.mu.se/pages/viewpage.action?pageId=838614955) — "
    "experiment [8045](https://www.ultimate-guitar.com/components/ab/experiment/view?id=8045), "
    "Iteration 1. Results calculated and published."
)
NOTIFICATION = (
    HEADING + "\n<@UDRI> <@UTEAM1>\n• Retention held on iOS.\n• iOS ad revenue fell."
)


class FakeReader:
    def __init__(self, storage: str, names: dict[str, str | None]) -> None:
        self.storage = storage
        self.names = names
        self.page_ids: list[str] = []

    async def page_storage(self, page_id: str) -> str:
        self.page_ids.append(page_id)
        return self.storage

    async def display_name(self, userkey: str) -> str | None:
        return self.names.get(userkey)


SLACK_IDS = {"Artyom Smirnov": "UDRI", "Aleksandr Trushevich": "UTEAM1", "Egor Semin": "UTEAM2"}
NAMES = {
    "key-dri": "Artyom Smirnov",
    "key-team-1": "Aleksandr Trushevich",
    "key-team-2": "Egor Semin",
}


async def fake_lookup(_client: object, name: str) -> str | None:
    return SLACK_IDS.get(name)


def test_header_userkeys_reads_only_dri_and_team_rows_in_order() -> None:
    assert header_userkeys(HEADER) == ("key-dri", "key-team-1", "key-team-2")


def test_header_userkeys_without_header_table() -> None:
    assert header_userkeys("<h1>Pitch</h1><p>No header</p>") == ()


def test_missing_mentions_skips_people_already_named() -> None:
    people = [
        HeaderPerson("key-dri", "Artyom Smirnov", "UDRI"),
        HeaderPerson("key-team-1", "Aleksandr Trushevich", "UTEAM1"),
        HeaderPerson("key-team-2", "Egor Semin", "UTEAM2"),
        HeaderPerson("key-plain", "Plain Person", None),
        HeaderPerson("key-unknown", None, None),
    ]
    assert missing_mentions(NOTIFICATION, people) == ["<@UTEAM2>", "Plain Person"]


def test_missing_mentions_accepts_labelled_mention_and_plain_name() -> None:
    text = HEADING + "\n<@UDRI|Artyom Smirnov> Egor Semin"
    people = [
        HeaderPerson("key-dri", "Artyom Smirnov", "UDRI"),
        HeaderPerson("key-team-2", "Egor Semin", "UTEAM2"),
    ]
    assert missing_mentions(text, people) == []


def test_add_mentions_extends_existing_mention_line() -> None:
    result = add_mentions(NOTIFICATION, ["<@UTEAM2>"])
    assert result.splitlines()[1] == "<@UDRI> <@UTEAM1> <@UTEAM2>"
    assert result.splitlines()[2] == "• Retention held on iOS."


def test_add_mentions_inserts_line_when_agent_wrote_none() -> None:
    text = HEADING + "\n• Retention held on iOS."
    result = add_mentions(text, ["<@UDRI>", "<@UTEAM2>"])
    assert result.splitlines() == [HEADING, "<@UDRI> <@UTEAM2>", "• Retention held on iOS."]


def test_add_mentions_without_missing_people_keeps_text() -> None:
    assert add_mentions(NOTIFICATION, []) == NOTIFICATION


async def test_header_people_falls_back_to_local_directory(tmp_path) -> None:
    directory = tmp_path / "context" / "confluence_userkey_to_display_name.json"
    directory.parent.mkdir()
    directory.write_text(json.dumps({"key-team-2": "Egor Semin"}), encoding="utf-8")
    reader = FakeReader(HEADER, {"key-dri": "Artyom Smirnov", "key-team-1": None})

    people = await header_people(
        SimpleNamespace(), reader, "838614955", tmp_path, slack_lookup=fake_lookup
    )

    assert people == [
        HeaderPerson("key-dri", "Artyom Smirnov", "UDRI"),
        HeaderPerson("key-team-1", None, None),
        HeaderPerson("key-team-2", "Egor Semin", "UTEAM2"),
    ]


def test_local_display_names_accepts_nested_users_and_ignores_missing_file(tmp_path) -> None:
    assert local_display_names(tmp_path) == {}
    directory = tmp_path / "context" / "confluence_userkey_to_display_name.json"
    directory.parent.mkdir()
    directory.write_text(json.dumps({"users": {"k": " Egor Semin "}}), encoding="utf-8")
    assert local_display_names(tmp_path) == {"k": "Egor Semin"}


async def test_ensure_header_mentions_adds_the_team_member_left_out(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "sloperator.project_page_people.DEFAULT_SLACK_MENTION_RESOLVER.user_id_for_name",
        fake_lookup,
    )
    reader = FakeReader(HEADER, NAMES)

    result = await ensure_header_mentions(
        SimpleNamespace(), NOTIFICATION, "838614955", tmp_path, reader=reader
    )

    assert result.splitlines()[1] == "<@UDRI> <@UTEAM1> <@UTEAM2>"
    assert reader.page_ids == ["838614955"]


async def test_ensure_header_mentions_fails_open(tmp_path) -> None:
    reader = FakeReader(HEADER, NAMES)
    reader.page_storage = AsyncMock(side_effect=RuntimeError("HTTP 503"))  # type: ignore[method-assign]

    result = await ensure_header_mentions(
        SimpleNamespace(), NOTIFICATION, "838614955", tmp_path, reader=reader
    )

    assert result == NOTIFICATION


async def test_ensure_header_mentions_without_page_or_token(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CONFLUENCE_PERSONAL_TOKEN", raising=False)
    monkeypatch.delenv("CONFLUENCE_TOKEN", raising=False)
    assert await ensure_header_mentions(SimpleNamespace(), NOTIFICATION, None, tmp_path) == (
        NOTIFICATION
    )
    assert await ensure_header_mentions(
        SimpleNamespace(), NOTIFICATION, "838614955", tmp_path
    ) == NOTIFICATION


def test_reader_from_environment(monkeypatch) -> None:
    monkeypatch.delenv("CONFLUENCE_PERSONAL_TOKEN", raising=False)
    monkeypatch.delenv("CONFLUENCE_TOKEN", raising=False)
    assert ConfluenceHeaderReader.from_environment() is None
    monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "pat")
    monkeypatch.delenv("CONFLUENCE_BASE_URL", raising=False)
    reader = ConfluenceHeaderReader.from_environment()
    assert reader is not None
    assert reader.base_url == "https://alice.mu.se"
    assert reader.headers["Authorization"] == "Bearer pat"


@pytest.mark.parametrize("page_id", ["838614955", None])
async def test_publish_run_posts_missing_header_mentions(monkeypatch, tmp_path, page_id) -> None:
    from sloperator.experiment_finalizer import publish_run

    seen_pages: list[str | None] = []

    async def fake_ensure(client, notification, project_page_id, workspace):
        seen_pages.append(project_page_id)
        assert workspace == tmp_path
        return add_mentions(notification, ["<@UTEAM2>"])

    monkeypatch.setattr("sloperator.experiment_finalizer.ensure_header_mentions", fake_ensure)
    client = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"channel": "C", "ts": "1"}))
    agent = SimpleNamespace(attach_session=AsyncMock())
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        experiment_finalizer_channel="CFINAL",
        agent_workspace=tmp_path,
    )
    run = HeadlessAgentRun("claude", "opus", "session-1", NOTIFICATION)

    result = await publish_run(client, agent, settings, run, project_page_id=page_id)

    # Without an explicit id the page is taken from the heading link.
    assert seen_pages == ["838614955"]
    assert result.splitlines()[1] == "<@UDRI> <@UTEAM1> <@UTEAM2>"
    assert client.chat_postMessage.await_args.kwargs["markdown_text"] == result
    assert agent.attach_session.await_args.args[2].text == result


async def test_publish_run_does_not_check_no_op(monkeypatch, tmp_path) -> None:
    from sloperator.experiment_finalizer import NO_OP_NOTIFICATION, publish_run

    ensure = AsyncMock()
    monkeypatch.setattr("sloperator.experiment_finalizer.ensure_header_mentions", ensure)
    client = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"channel": "C", "ts": "1"}))
    agent = SimpleNamespace(attach_session=AsyncMock())
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        experiment_finalizer_channel="CFINAL",
        agent_workspace=tmp_path,
    )
    run = HeadlessAgentRun("claude", "opus", "session-1", NO_OP_NOTIFICATION)

    assert await publish_run(client, agent, settings, run) == NO_OP_NOTIFICATION
    ensure.assert_not_awaited()
