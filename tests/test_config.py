from __future__ import annotations

import pytest

from sloperator.config import ConfigurationError, Settings


def test_settings_load_valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_USER_ID", "U1234567890")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID", "xapp-test")
    monkeypatch.setenv("SLOPERATOR_PORT", "9000")

    settings = Settings.from_environment()

    assert settings.port == 9000


def test_settings_reject_invalid_app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_USER_ID", "U1234567890")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID", "not-an-app-token")

    with pytest.raises(ConfigurationError, match="Socket Mode"):
        Settings.from_environment()
