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
