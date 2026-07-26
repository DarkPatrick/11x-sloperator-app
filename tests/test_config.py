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
    assert settings.anomaly_alert_channel == "C06FADPMGKT"
    assert settings.subscription_flow_alert_channel == "C06FADPMGKT"


def test_settings_load_clickhouse_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_USER_ID", "U1234567890")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID", "xapp-test")
    monkeypatch.setenv("CLICKHOUSE_HOST", "clickhouse.internal")
    monkeypatch.setenv("CLICKHOUSE_PORT", "8443")
    monkeypatch.setenv("CLICKHOUSE_USERNAME", "reader")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")

    settings = Settings.from_environment()

    assert settings.clickhouse_host == "clickhouse.internal"
    assert settings.clickhouse_port == 8443
    assert settings.clickhouse_username == "reader"
    assert settings.clickhouse_password == "secret"


def test_settings_reject_invalid_app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_USER_ID", "U1234567890")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID", "not-an-app-token")

    with pytest.raises(ConfigurationError, match="Socket Mode"):
        Settings.from_environment()
