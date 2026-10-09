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
                            "display_name": "Jon Sample",
                            "real_name": "Jonathan Sample",
                        },
                    },
                    {
                        "id": "U0PATDOE1",
                        "profile": {"display_name": "", "real_name": "Pat Doe"},
                    },
                ]
            )
        ),
    )
    resolver = SlackMentionResolver()

    result = await resolver.resolve(
        client,
        "Pat Doe Jon Sample (no Slack id could be resolved for him)",
    )

    assert result == "<@U0PATDOE1> <@U0BDSSHNUDU>"


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
                    {"id": "U4", "profile": {"real_name": "Sam Roe"}},
                ]
            ),
        ]
    )
    resolver = SlackMentionResolver()

    first = await resolver.resolve(client, "Alex Smith, Sam Roe and Old User")
    second = await resolver.resolve(client, "Sam Roe")

    assert first == "Alex Smith, <@U4> and Old User"
    assert second == "<@U4>"
    assert client.users_list.await_count == 2


async def test_resolver_preserves_links_code_and_existing_mentions() -> None:
    client = SimpleNamespace(
        token="xoxb-test",
        users_list=AsyncMock(
            return_value=_page(
                [{"id": "U1", "profile": {"real_name": "Jon Sample"}}]
            )
        ),
    )
    resolver = SlackMentionResolver()
    text = (
        "Jon Sample <@U1|Jon Sample> "
        "[Jon Sample](https://example.com) `Jon Sample`"
    )

    assert await resolver.resolve(client, text) == (
        "<@U1> <@U1|Jon Sample> "
        "[Jon Sample](https://example.com) `Jon Sample`"
    )


async def test_resolver_uses_private_exact_aliases(tmp_path) -> None:
    aliases = tmp_path / "slack-identity-aliases.json"
    aliases.write_text(
        '{"aliases": {"Katherine Example": "U0ALIAS01"}}',
        encoding="utf-8",
    )
    client = SimpleNamespace(
        token="xoxb-test",
        users_list=AsyncMock(
            return_value=_page(
                [
                    {
                        "id": "U0ALIAS01",
                        "profile": {
                            "display_name": "Kate Example",
                            "real_name": "Kate Example",
                        },
                    }
                ]
            )
        ),
    )
    resolver = SlackMentionResolver(private_aliases_path=aliases)

    assert await resolver.resolve(client, "Katherine Example and Kate Example") == (
        "<@U0ALIAS01> and <@U0ALIAS01>"
    )


async def test_payload_resolution_fails_open(monkeypatch) -> None:
    resolver = SimpleNamespace(resolve=AsyncMock(side_effect=RuntimeError("Slack unavailable")))
    monkeypatch.setattr(
        "sloperator.slack_identity.DEFAULT_SLACK_MENTION_RESOLVER",
        resolver,
    )
    payload = {"channel": "C1", "text": "Jon Sample"}

    assert await resolve_payload_mentions(SimpleNamespace(), payload) == payload


async def test_payload_resolves_block_text(monkeypatch) -> None:
    resolver = SimpleNamespace(
        resolve=AsyncMock(side_effect=lambda _client, text: text.replace("Jon Sample", "<@U1>"))
    )
    monkeypatch.setattr(
        "sloperator.slack_identity.DEFAULT_SLACK_MENTION_RESOLVER",
        resolver,
    )
    payload = {
        "channel": "C1",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Jon Sample"}}],
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
            "text": payload["text"].replace("Jon Sample", "<@U1>"),
        }
    )
    monkeypatch.setattr(AsyncWebClient, "api_call", api_call)
    monkeypatch.setattr("sloperator.operations_slack.resolve_payload_mentions", resolve)
    monkeypatch.setattr("sloperator.operations_slack.emit_runtime", lambda *args: None)
    client = ObservedSlackClient(token="xoxb-test")

    await client.chat_postMessage(channel="C1", text="Jon Sample")

    assert api_call.await_args.args == ("chat.postMessage",)
    assert api_call.await_args.kwargs["json"]["text"] == "<@U1>"


def test_default_aliases_path_prefers_env_then_working_directory(tmp_path, monkeypatch) -> None:
    from sloperator import slack_identity

    configured = tmp_path / "custom.json"
    monkeypatch.setenv(slack_identity.PRIVATE_ALIASES_ENV, str(configured))
    assert slack_identity.default_private_aliases_path() == configured

    monkeypatch.delenv(slack_identity.PRIVATE_ALIASES_ENV)
    monkeypatch.setattr(slack_identity, "__file__", str(tmp_path / "site" / "pkg" / "mod.py"))
    monkeypatch.chdir(tmp_path)
    assert slack_identity.default_private_aliases_path() == (
        tmp_path / "data" / "slack-identity-aliases.json"
    )
