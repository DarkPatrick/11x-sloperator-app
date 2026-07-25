"""Validated application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when required application configuration is invalid."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings loaded from the environment."""

    slack_user_id: str
    bot_token: str
    app_token: str
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"
    database_path: Path = Path("data/sloperator.sqlite3")
    backfill_limit: int = 100
    sync_interval_seconds: int = 300

    @classmethod
    def from_environment(cls) -> Settings:
        """Load a validated settings object, including a local ``.env`` file."""
        load_dotenv()

        user_id = _required("SLACK_USER_ID")
        bot_token = _required("SLOPERATOR_SLACK_BOT_TOKEN")
        app_token = _required("SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID")
        host = os.environ.get("SLOPERATOR_HOST", "127.0.0.1").strip()
        log_level = os.environ.get("SLOPERATOR_LOG_LEVEL", "INFO").strip().upper()
        database_path = Path(
            os.environ.get("SLOPERATOR_DATABASE_PATH", "data/sloperator.sqlite3")
        ).expanduser()

        try:
            port = int(os.environ.get("SLOPERATOR_PORT", "8080"))
            backfill_limit = int(os.environ.get("SLOPERATOR_BACKFILL_LIMIT", "100"))
            sync_interval_seconds = int(os.environ.get("SLOPERATOR_SYNC_INTERVAL_SECONDS", "300"))
        except ValueError as error:
            raise ConfigurationError(
                "Port, backfill limit, and sync interval must be integers"
            ) from error

        if not user_id.startswith("U"):
            raise ConfigurationError("SLACK_USER_ID must be a Slack user ID")
        if not bot_token.startswith("xoxb-"):
            raise ConfigurationError("SLOPERATOR_SLACK_BOT_TOKEN must be a bot token")
        if not app_token.startswith("xapp-"):
            raise ConfigurationError(
                "SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID must contain the Socket Mode app token"
            )
        if not 1 <= port <= 65535:
            raise ConfigurationError("SLOPERATOR_PORT must be between 1 and 65535")
        if not 0 <= backfill_limit <= 1_000:
            raise ConfigurationError("SLOPERATOR_BACKFILL_LIMIT must be between 0 and 1000")
        if not 60 <= sync_interval_seconds <= 86_400:
            raise ConfigurationError(
                "SLOPERATOR_SYNC_INTERVAL_SECONDS must be between 60 and 86400"
            )
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("SLOPERATOR_LOG_LEVEL is invalid")

        return cls(
            slack_user_id=user_id,
            bot_token=bot_token,
            app_token=app_token,
            host=host,
            port=port,
            log_level=log_level,
            database_path=database_path,
            backfill_limit=backfill_limit,
            sync_interval_seconds=sync_interval_seconds,
        )
