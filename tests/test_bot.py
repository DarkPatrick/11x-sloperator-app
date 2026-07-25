from sloperator.bot import normalize_command, reply_thread_ts, response_for


def test_normalize_command_removes_mention_and_whitespace() -> None:
    assert normalize_command("  <@U123ABC>   PiNg  ") == "ping"


def test_response_for_ping() -> None:
    assert response_for("ping") == "pong"


def test_response_for_unknown_command() -> None:
    assert "Unknown command" in response_for("something else")


def test_reply_thread_ts_preserves_active_chat_thread() -> None:
    assert reply_thread_ts({"thread_ts": "1784999083.602329"}) == "1784999083.602329"


def test_reply_thread_ts_is_none_for_top_level_dm() -> None:
    assert reply_thread_ts({"ts": "1784999406.466489"}) is None
