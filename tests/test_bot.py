from sloperator.bot import normalize_command, response_for


def test_normalize_command_removes_mention_and_whitespace() -> None:
    assert normalize_command("  <@U123ABC>   PiNg  ") == "ping"


def test_response_for_ping() -> None:
    assert response_for("ping") == "pong"


def test_response_for_unknown_command() -> None:
    assert "Unknown command" in response_for("something else")
