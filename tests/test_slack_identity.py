from types import SimpleNamespace
from unittest.mock import AsyncMock

from slack_sdk.web.async_client import AsyncWebClient

from sloperator.operations_slack import ObservedSlackClient
from sloperator.slack_identity import SlackMentionResolver, resolve_payload_mentions


def _page(members, cursor=""):
    return {
        "members": members,
        "response_metadata": {"next_cursor": cursor},
    }


async def test_exact_profile_names_become_mentions_and_gap_note_is_removed() -> None:
    client = SimpleNamespace(
        token="xoxb-test",
        users_list=AsyncMock(
            return_value=_page(
                [
                    {
                        "id": "U0BDSSHNUDU",
                        "profile": {
                            "display_name": "Misha Tcymlov",
                            "real_name": "Mikhail Tcymlov",
                        },
                    },
                    {
                        "id": "U0525MDT0MN",
                        "profile": {"display_name": "", "real_name": "Egor Semin"},
                    },
                ]
            )
        ),
    )
    resolver = SlackMentionResolver()

    result = await resolver.resolve(
        client,
        "Egor Semin Misha Tcymlov (no Slack id could be resolved for him)",
    )

    assert result == "<@U0525MDT0MN> <@U0BDSSHNUDU>"


async def test_resolver_paginates_caches_and_skips_ambiguous_or_deleted_users() -> None:
    client = SimpleNamespace(token="xoxb-test")
    client.users_list = AsyncMock(
        side_effect=[
            _page(
                [
                    {"id": "U1", "profile": {"real_name": "Alex Smith"}},
                    {"id": "U2", "deleted": True, "profile": {"real_name": "Old User"}},
                ],
                "page-2",
            ),
            _page(
                [
                    {"id": "U3", "profile": {"real_name": "Alex Smith"}},
                    {"id": "U4", "profile": {"real_name": "Artyom Smirnov"}},
                ]
            ),
        ]
    )
    resolver = SlackMentionResolver()

    first = await resolver.resolve(client, "Alex Smith, Artyom Smirnov and Old User")
    second = await resolver.resolve(client, "Artyom Smirnov")

    assert first == "Alex Smith, <@U4> and Old User"
    assert second == "<@U4>"
    assert client.users_list.await_count == 2


async def test_resolver_preserves_links_code_and_existing_mentions() -> None:
    client = SimpleNamespace(
        token="xoxb-test",
        users_list=AsyncMock(
            return_value=_page(
                [{"id": "U1", "profile": {"real_name": "Misha Tcymlov"}}]
            )
        ),
    )
    resolver = SlackMentionResolver()
    text = (
        "Misha Tcymlov <@U1|Misha Tcymlov> "
        "[Misha Tcymlov](https://example.com) `Misha Tcymlov`"
    )

    assert await resolver.resolve(client, text) == (
        "<@U1> <@U1|Misha Tcymlov> "
        "[Misha Tcymlov](https://example.com) `Misha Tcymlov`"
    )


async def test_payload_resolution_fails_open(monkeypatch) -> None:
    resolver = SimpleNamespace(resolve=AsyncMock(side_effect=RuntimeError("Slack unavailable")))
    monkeypatch.setattr(
        "sloperator.slack_identity.DEFAULT_SLACK_MENTION_RESOLVER",
        resolver,
    )
    payload = {"channel": "C1", "text": "Misha Tcymlov"}

    assert await resolve_payload_mentions(SimpleNamespace(), payload) == payload


async def test_payload_resolves_block_text(monkeypatch) -> None:
    resolver = SimpleNamespace(
        resolve=AsyncMock(side_effect=lambda _client, text: text.replace("Misha Tcymlov", "<@U1>"))
    )
    monkeypatch.setattr(
        "sloperator.slack_identity.DEFAULT_SLACK_MENTION_RESOLVER",
        resolver,
    )
    payload = {
        "channel": "C1",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Misha Tcymlov"}}],
    }

    assert await resolve_payload_mentions(SimpleNamespace(), payload) == {
        "channel": "C1",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "<@U1>"}}],
    }


async def test_observed_client_resolves_before_every_message_delivery(monkeypatch) -> None:
    api_call = AsyncMock(return_value={"channel": "C1", "ts": "1.1"})
    resolve = AsyncMock(
        side_effect=lambda _client, payload: {
            **payload,
            "text": payload["text"].replace("Misha Tcymlov", "<@U1>"),
        }
    )
    monkeypatch.setattr(AsyncWebClient, "api_call", api_call)
    monkeypatch.setattr("sloperator.operations_slack.resolve_payload_mentions", resolve)
    monkeypatch.setattr("sloperator.operations_slack.emit_runtime", lambda *args: None)
    client = ObservedSlackClient(token="xoxb-test")

    await client.chat_postMessage(channel="C1", text="Misha Tcymlov")

    assert api_call.await_args.args == ("chat.postMessage",)
    assert api_call.await_args.kwargs["json"]["text"] == "<@U1>"
