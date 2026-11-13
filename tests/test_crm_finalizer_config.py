"""CRM finalizer settings: crm_finalizer_enabled and crm_finalizer_hour."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from sloperator import config
from sloperator.config import ConfigurationError, Settings

INTEGER_ERROR = "Port, archive limits, and agent limits must be integers"
CRM_HOUR_ERROR = "CRM_FINALIZER_HOUR must be between 0 and 23"


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Minimal environment with which Settings.from_environment() succeeds."""
    # Keep a local .env file from leaking finalizer variables into the cases.
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("SLACK_USER_ID", "U1234567890")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID", "xapp-test")
    monkeypatch.setenv("SLACK_ALLOWED_CONVERSATION_USERS", "")
    for name in (
        "CRM_FINALIZER_ENABLED",
        "CRM_FINALIZER_HOUR",
        "EXPERIMENT_FINALIZER_ENABLED",
        "EXPERIMENT_FINALIZER_HOUR",
        "EXPERIMENT_FINALIZER_TIMEZONE",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_c1_defaults_from_environment(base_env: pytest.MonkeyPatch) -> None:
    settings = Settings.from_environment()

    assert settings.crm_finalizer_enabled is True
    assert settings.crm_finalizer_hour == 14
    assert settings.experiment_finalizer_hour == 12
    assert settings.experiment_finalizer_enabled is True


def test_c2_dataclass_defaults(tmp_path: Path) -> None:
    settings = Settings(
        slack_user_id="UOWNER",
        bot_token="xoxb-test",
        app_token="xapp-test",
        database_path=tmp_path / "sloperator.sqlite3",
    )

    assert settings.crm_finalizer_enabled is True
    assert settings.crm_finalizer_hour == 14


def test_c3_configured_crm_hour(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("CRM_FINALIZER_HOUR", "9")

    settings = Settings.from_environment()

    assert settings.crm_finalizer_hour == 9
    assert settings.experiment_finalizer_hour == 12


def test_c4_crm_hour_does_not_follow_ugm_hour(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("EXPERIMENT_FINALIZER_HOUR", "10")

    settings = Settings.from_environment()

    assert settings.experiment_finalizer_hour == 10
    assert settings.crm_finalizer_hour == 14


def test_c5_crm_disabled_ugm_enabled(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("CRM_FINALIZER_ENABLED", "false")

    settings = Settings.from_environment()

    assert settings.crm_finalizer_enabled is False
    assert settings.experiment_finalizer_enabled is True


def test_c6_ugm_disabled_crm_enabled(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("EXPERIMENT_FINALIZER_ENABLED", "false")

    settings = Settings.from_environment()

    assert settings.experiment_finalizer_enabled is False
    assert settings.crm_finalizer_enabled is True


@pytest.mark.parametrize(
    ("case_id", "value"),
    [("C7", "24"), ("C8", "-1")],
)
def test_c7_c8_crm_hour_out_of_range(
    base_env: pytest.MonkeyPatch, case_id: str, value: str
) -> None:
    base_env.setenv("CRM_FINALIZER_HOUR", value)

    with pytest.raises(ConfigurationError) as error:
        Settings.from_environment()

    assert str(error.value) == CRM_HOUR_ERROR, case_id


def test_c9_crm_hour_bounds_inclusive(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("CRM_FINALIZER_HOUR", "0")
    assert Settings.from_environment().crm_finalizer_hour == 0

    base_env.setenv("CRM_FINALIZER_HOUR", "23")
    assert Settings.from_environment().crm_finalizer_hour == 23


@pytest.mark.parametrize("value", ["1", "TRUE", " yes ", "On"])
def test_c10_crm_enabled_accepted_values(base_env: pytest.MonkeyPatch, value: str) -> None:
    base_env.setenv("CRM_FINALIZER_ENABLED", value)

    assert Settings.from_environment().crm_finalizer_enabled is True


def test_c11_only_two_crm_fields_and_finalizer_timezone(
    base_env: pytest.MonkeyPatch,
) -> None:
    base_env.setenv("EXPERIMENT_FINALIZER_TIMEZONE", "Europe/London")

    settings = Settings.from_environment()

    crm_fields = {
        field.name
        for field in dataclasses.fields(Settings)
        if field.name.startswith("crm_finalizer_")
    }
    assert crm_fields == {"crm_finalizer_enabled", "crm_finalizer_hour"}
    assert settings.experiment_finalizer_timezone == "Europe/London"


# C12 (existing suites pass) is covered by running the whole BOT test suite, not by a test here.


@pytest.mark.parametrize(
    ("case_id", "value"),
    [("E1", "abc"), ("E2", "14.5"), ("E3", "")],
)
def test_e1_e2_e3_non_integer_crm_hour(
    base_env: pytest.MonkeyPatch, case_id: str, value: str
) -> None:
    base_env.setenv("CRM_FINALIZER_HOUR", value)

    with pytest.raises(ConfigurationError) as error:
        Settings.from_environment()

    assert str(error.value) == INTEGER_ERROR, case_id


def test_e4_crm_hour_with_whitespace(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("CRM_FINALIZER_HOUR", " 15 ")

    assert Settings.from_environment().crm_finalizer_hour == 15


def test_e5_crm_hour_error_is_crm_specific(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("CRM_FINALIZER_HOUR", "25")
    base_env.setenv("EXPERIMENT_FINALIZER_HOUR", "12")

    with pytest.raises(ConfigurationError) as error:
        Settings.from_environment()

    assert str(error.value) == CRM_HOUR_ERROR


@pytest.mark.parametrize("value", ["0", "no", "off", "maybe"])
def test_e6_crm_enabled_other_values_are_off(
    base_env: pytest.MonkeyPatch, value: str
) -> None:
    base_env.setenv("CRM_FINALIZER_ENABLED", value)

    assert Settings.from_environment().crm_finalizer_enabled is False


def test_e7_empty_enabled_matches_ugm_rule(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("CRM_FINALIZER_ENABLED", "")
    crm_enabled = Settings.from_environment().crm_finalizer_enabled

    base_env.delenv("CRM_FINALIZER_ENABLED")
    base_env.setenv("EXPERIMENT_FINALIZER_ENABLED", "")
    ugm_enabled = Settings.from_environment().experiment_finalizer_enabled

    assert crm_enabled == ugm_enabled


def test_e8_same_hour_allowed_and_read_on_each_call(
    base_env: pytest.MonkeyPatch,
) -> None:
    base_env.setenv("CRM_FINALIZER_HOUR", "12")
    first = Settings.from_environment()
    assert (first.crm_finalizer_hour, first.experiment_finalizer_hour) == (12, 12)

    base_env.setenv("CRM_FINALIZER_HOUR", "16")
    assert Settings.from_environment().crm_finalizer_hour == 16
