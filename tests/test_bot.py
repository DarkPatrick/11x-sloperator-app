from unittest.mock import AsyncMock, MagicMock

from sloperator.archive import conversation_kind, event_channel_id
from sloperator.bot import (
    create_app,
    is_trusted_channel_thread,
    is_vpn_command,
    normalize_command,
    reply_thread_ts,
    response_for,
    vpn_otp_from_command,
)
from sloperator.config import Settings
from sloperator.store import EventStore


def test_normalize_command_removes_mention_and_whitespace() -> None:
    assert normalize_command("  <@U123ABC>   PiNg  ") == "ping"


def test_response_for_ping() -> None:
    assert response_for("ping") == "pong"


def test_response_for_unknown_command() -> None:
    assert "Unknown command" in response_for("something else")


def test_vpn_otp_is_reserved_independently_of_vpn_state() -> None:
    assert vpn_otp_from_command("123456") == "123456"
    assert vpn_otp_from_command("vpn otp 12345678") == "12345678"
    assert vpn_otp_from_command("12345") is None
    assert vpn_otp_from_command("analyze 123456") is None


def test_vpn_commands_are_reserved_inside_dm_threads() -> None:
    assert is_vpn_command("vpn ready")
    assert is_vpn_command("готов")
    assert is_vpn_command("vpn status")
    assert is_vpn_command("123456")
    assert not is_vpn_command("проверь vpn проблему")


def test_reply_thread_ts_preserves_active_chat_thread() -> None:
    assert reply_thread_ts({"thread_ts": "1784999083.602329"}) == "1784999083.602329"


def test_reply_thread_ts_is_none_for_top_level_dm() -> None:
    assert reply_thread_ts({"ts": "1784999406.466489"}) is None


def test_event_channel_id_supports_messages_and_channel_events() -> None:
    assert event_channel_id({"channel": "D123"}) == "D123"
    assert event_channel_id({"channel": {"id": "C123"}}) == "C123"
    assert event_channel_id({"item": {"channel": "C456"}}) == "C456"


def test_conversation_kind_identifies_direct_conversations() -> None:
    assert conversation_kind({"is_im": True}) == "direct"
    assert conversation_kind({"is_mpim": True}) == "direct"
    assert conversation_kind({"is_private": True}) == "channel"


def test_trusted_channel_thread_requires_owner_monitoring_channel_and_thread() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        slack_allowed_conversation_users=frozenset({"UOWNER", "UANALYST"}),
    )
    event = {
        "user": "UOWNER",
        "channel": settings.anomaly_alert_channel,
        "thread_ts": "100.1",
        "ts": "100.2",
        "text": "Проверь ещё платформы",
    }

    assert is_trusted_channel_thread(event, settings)
    assert is_trusted_channel_thread({**event, "user": "UANALYST"}, settings)
    assert not is_trusted_channel_thread({**event, "user": "UOTHER"}, settings)
    assert not is_trusted_channel_thread({**event, "channel": "COTHER"}, settings)
    assert not is_trusted_channel_thread({**event, "thread_ts": None}, settings)


def test_trusted_channel_thread_supports_subscription_flow_channel() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        slack_allowed_conversation_users=frozenset({"UOWNER"}),
        subscription_flow_alert_channel="CSUBFLOW",
    )
    event = {
        "user": "UOWNER",
        "channel": "CSUBFLOW",
        "thread_ts": "100.1",
        "ts": "100.2",
        "text": "Что изменилось после recovery?",
    }

    assert is_trusted_channel_thread(event, settings)


def test_trusted_channel_thread_supports_experiment_finalizer_channel() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        slack_allowed_conversation_users=frozenset({"UOWNER", "UANALYST"}),
        experiment_finalizer_channel="CFINAL",
    )
    event = {
        "user": "UANALYST",
        "channel": "CFINAL",
        "thread_ts": "100.1",
        "ts": "100.2",
        "text": "Which segment drove the effect?",
    }

    assert is_trusted_channel_thread(event, settings)


def test_trusted_channel_thread_supports_mobile_health_channel() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        slack_allowed_conversation_users=frozenset({"UOWNER"}),
    )
    event = {
        "user": "UOWNER",
        "channel": settings.mobile_health_alert_channel,
        "thread_ts": "100.1",
        "ts": "100.2",
        "text": "Проверь влияние версии приложения",
    }

    assert is_trusted_channel_thread(event, settings)


def test_trusted_channel_thread_supports_allowlisted_user_in_agent_dm() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        slack_allowed_conversation_users=frozenset({"UOWNER", "UANALYST"}),
    )
    event = {
        "user": "UANALYST",
        "channel": "DANALYST",
        "channel_type": "im",
        "thread_ts": "100.1",
        "ts": "100.2",
        "text": "Почему именно эти segments?",
    }

    assert is_trusted_channel_thread(event, settings)
    assert not is_trusted_channel_thread({**event, "user": "UOTHER"}, settings)


def test_settings_without_explicit_conversation_users_fall_back_to_owner() -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    event = {
        "user": "UOWNER",
        "channel": settings.anomaly_alert_channel,
        "thread_ts": "100.1",
        "ts": "100.2",
        "text": "Продолжай",
    }

    assert is_trusted_channel_thread(event, settings)


async def test_reaction_events_are_acknowledged_without_starting_work(tmp_path) -> None:
    settings = Settings("UOWNER", "xoxb-test", "xapp-test")
    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    orchestrator = MagicMock()
    app = create_app(settings, store, orchestrator, MagicMock())

    reaction_handlers = app._async_listeners[2:4]
    assert len(reaction_handlers) == 2
    for listener, event_type in zip(
        reaction_handlers,
        ("reaction_removed", "reaction_added"),
        strict=True,
    ):
        await listener.ack_function(event={"type": event_type}, client=AsyncMock())

    orchestrator.submit.assert_not_called()


async def test_thinking_reaction_on_bot_answer_queues_recheck(tmp_path) -> None:
    settings = Settings("UOWNER", "xoxb-test", "xapp-test")
    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.message_context = MagicMock(return_value={"thread_ts": "100.1"})  # type: ignore[method-assign]
    store.has_agent_thread = MagicMock(return_value=True)  # type: ignore[method-assign]
    orchestrator = MagicMock()
    orchestrator.submit = AsyncMock()
    app = create_app(settings, store, orchestrator, MagicMock())
    client = AsyncMock()
    client.auth_test.return_value = {"user_id": "UBOT"}
    reaction_added = app._async_listeners[3]

    await reaction_added.ack_function(
        event={
            "type": "reaction_added",
            "user": "UOWNER",
            "reaction": "thinking_face",
            "item_user": "UBOT",
            "event_ts": "100.3",
            "item": {"type": "message", "channel": "D123", "ts": "100.2"},
        },
        client=client,
    )

    orchestrator.submit.assert_awaited_once()
    kwargs = orchestrator.submit.await_args.kwargs
    assert kwargs["thread_ts"] == "100.1"
    assert kwargs["message_ts"] == "100.3"
    assert kwargs["react_to_message"] is False


async def test_non_thinking_reaction_does_not_start_work(tmp_path) -> None:
    settings = Settings("UOWNER", "xoxb-test", "xapp-test")
    store = EventStore(tmp_path / "state.sqlite3")
    store.initialize()
    orchestrator = MagicMock()
    orchestrator.submit = AsyncMock()
    app = create_app(settings, store, orchestrator, MagicMock())

    await app._async_listeners[3].ack_function(
        event={"type": "reaction_added", "user": "UOWNER", "reaction": "eyes"},
        client=AsyncMock(),
    )

    orchestrator.submit.assert_not_awaited()
