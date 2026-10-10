from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sloperator.archive import synchronize_archive
from sloperator.store import EventStore


def _response(**data: object) -> SimpleNamespace:
    return SimpleNamespace(data=data)


@pytest.mark.asyncio
async def test_history_sync_dispatches_each_new_message_only_once(tmp_path) -> None:
    store = EventStore(tmp_path / "archive.sqlite3")
    store.initialize()
    message = {
        "ts": "100.1",
        "user": "UBOT",
        "bot_id": "BSELF",
        "text": ":rotating_light: *SERIOUS — Web renewals anomaly*",
    }
    client = SimpleNamespace(
        auth_test=AsyncMock(
            return_value={"team_id": "T1", "team": "Test", "user_id": "UBOT"}
        ),
        conversations_list=AsyncMock(
            side_effect=[
                _response(
                    channels=[
                        {
                            "id": "C1",
                            "name": "monitoring",
                            "is_member": True,
                        }
                    ],
                    response_metadata={"next_cursor": ""},
                ),
                _response(channels=[], response_metadata={"next_cursor": ""}),
                _response(
                    channels=[
                        {
                            "id": "C1",
                            "name": "monitoring",
                            "is_member": True,
                        }
                    ],
                    response_metadata={"next_cursor": ""},
                ),
                _response(channels=[], response_metadata={"next_cursor": ""}),
            ]
        ),
        conversations_history=AsyncMock(return_value=_response(messages=[message])),
    )
    handler = AsyncMock()

    await synchronize_archive(client, store, 100, on_new_message=handler)
    await synchronize_archive(client, store, 100, on_new_message=handler)

    handler.assert_awaited_once_with("C1", message)
    assert store.contains_message("C1", "100.1")


def _channels_client(history: list[SimpleNamespace], replies: AsyncMock) -> SimpleNamespace:
    channel = {"id": "C1", "name": "monitoring", "is_member": True}
    listings = []
    for _ in history:
        listings += [
            _response(channels=[channel], response_metadata={"next_cursor": ""}),
            _response(channels=[], response_metadata={"next_cursor": ""}),
        ]
    return SimpleNamespace(
        auth_test=AsyncMock(return_value={"team_id": "T1", "team": "Test", "user_id": "UBOT"}),
        conversations_list=AsyncMock(side_effect=listings),
        conversations_history=AsyncMock(side_effect=history),
        conversations_replies=replies,
    )


@pytest.mark.asyncio
async def test_history_sync_dispatches_new_thread_replies_once(tmp_path) -> None:
    store = EventStore(tmp_path / "archive.sqlite3")
    store.initialize()
    alert = {
        "ts": "100.1",
        "user": "UBOT",
        "bot_id": "BSELF",
        "text": ":rotating_light: *SERIOUS — Android recurring charges anomaly*",
    }
    alert_with_reply = {**alert, "thread_ts": "100.1", "reply_count": 1, "latest_reply": "100.2"}
    recovered = {
        "ts": "100.2",
        "thread_ts": "100.1",
        "user": "UBOT",
        "bot_id": "BSELF",
        "text": ":white_check_mark: *Recovered — Android recurring charges (renewed)*",
    }
    replies = AsyncMock(return_value=_response(messages=[alert_with_reply, recovered]))
    client = _channels_client(
        [
            _response(messages=[alert]),
            _response(messages=[alert_with_reply]),
            _response(messages=[alert_with_reply]),
        ],
        replies,
    )
    handler = AsyncMock()

    for _ in range(3):
        await synchronize_archive(client, store, 100, on_new_message=handler)

    assert [call.args for call in handler.await_args_list] == [("C1", alert), ("C1", recovered)]
    assert store.contains_message("C1", "100.2")
    replies.assert_awaited_once()
