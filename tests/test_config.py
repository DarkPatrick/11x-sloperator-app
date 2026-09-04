from __future__ import annotations

import pytest

from sloperator.config import ConfigurationError, Settings


def test_settings_load_valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_USER_ID", "U1234567890")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID", "xapp-test")
    monkeypatch.setenv("SLOPERATOR_PORT", "9000")
    monkeypatch.setenv("SLACK_ALLOWED_CONVERSATION_USERS", "")

    settings = Settings.from_environment()

    assert settings.port == 9000
    assert settings.anomaly_alert_channel == "C06FADPMGKT"
    assert settings.subscription_flow_alert_channel == "C06FADPMGKT"
    assert settings.mobile_health_alert_channel == "C0AJKHFHVHV"
    assert settings.mobile_health_bot_id == "B0AM51CS2H5"
    assert settings.slack_allowed_conversation_users == {"U1234567890"}
    assert settings.experiment_finalizer_enabled is True
    assert settings.experiment_finalizer_timezone == "Asia/Nicosia"
    assert settings.experiment_finalizer_hour == 12
    assert settings.experiment_finalizer_timeout_seconds == 7_200
    assert settings.experiment_finalizer_channel == "C07A9FDQ14P"
    assert settings.experiment_analytics_enabled is True
    assert settings.experiment_analytics_timezone == "Asia/Nicosia"
    assert settings.experiment_analytics_hour == 16
    assert settings.experiment_analytics_timeout_seconds == 7_200
    assert settings.experiment_analytics_channel == "C07A9FDQ14P"
    assert settings.experiment_config_timeout_seconds == 7_200
    assert settings.mobile_health_timeout_seconds == 3_600


def test_settings_parse_bracketed_allowed_conversation_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_USER_ID", "UOWNER")
    monkeypatch.setenv(
        "SLACK_ALLOWED_CONVERSATION_USERS",
        "[UOWNER, UANALYST1, UANALYST2, UANALYST3, UANALYST4]",
    )
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID", "xapp-test")

    settings = Settings.from_environment()

    assert settings.slack_allowed_conversation_users == {
        "UOWNER",
        "UANALYST1",
        "UANALYST2",
        "UANALYST3",
        "UANALYST4",
    }


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
