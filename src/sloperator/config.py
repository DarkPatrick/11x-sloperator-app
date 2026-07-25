"""Validated application configuration."""

from __future__ import annotations

import os
from contextlib import suppress
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
    agent_workspace: Path = Path("/home/egor/projects/ug-ai-analyst")
    default_agent: str = "claude"
    claude_model: str = "opus"
    codex_model: str = "gpt-5.6-sol"
    claude_cli: Path = Path("/home/egor/.local/bin/claude")
    codex_cli: Path = Path("/usr/bin/codex")
    agent_timeout_seconds: int = 1_800
    agent_max_concurrency: int = 2

    @classmethod
    def from_environment(cls) -> Settings:
        """Load a validated settings object, including a local ``.env`` file."""
        # Production systemd loads EnvironmentFile before making the secret
        # file inaccessible to this process and all spawned agent CLIs.
        with suppress(PermissionError):
            load_dotenv()

        user_id = _required("SLACK_USER_ID")
        bot_token = _required("SLOPERATOR_SLACK_BOT_TOKEN")
        app_token = _required("SLOPERATOR_SLACK_BOT_SOCKET_TOKEN_ID")
        host = os.environ.get("SLOPERATOR_HOST", "127.0.0.1").strip()
        log_level = os.environ.get("SLOPERATOR_LOG_LEVEL", "INFO").strip().upper()
        database_path = Path(
            os.environ.get("SLOPERATOR_DATABASE_PATH", "data/sloperator.sqlite3")
        ).expanduser()
        agent_workspace = Path(
            os.environ.get(
                "SLOPERATOR_AGENT_WORKSPACE",
                "/home/egor/projects/ug-ai-analyst",
            )
        ).expanduser()
        default_agent = os.environ.get("SLOPERATOR_DEFAULT_AGENT", "claude").strip().lower()
        claude_model = os.environ.get("SLOPERATOR_CLAUDE_MODEL", "opus").strip()
        codex_model = os.environ.get("SLOPERATOR_CODEX_MODEL", "gpt-5.6-sol").strip()
        claude_cli = Path(
            os.environ.get("SLOPERATOR_CLAUDE_CLI", "/home/egor/.local/bin/claude")
        ).expanduser()
        codex_cli = Path(os.environ.get("SLOPERATOR_CODEX_CLI", "/usr/bin/codex")).expanduser()

        try:
            port = int(os.environ.get("SLOPERATOR_PORT", "8080"))
            backfill_limit = int(os.environ.get("SLOPERATOR_BACKFILL_LIMIT", "100"))
            sync_interval_seconds = int(os.environ.get("SLOPERATOR_SYNC_INTERVAL_SECONDS", "300"))
            agent_timeout_seconds = int(os.environ.get("SLOPERATOR_AGENT_TIMEOUT_SECONDS", "1800"))
            agent_max_concurrency = int(os.environ.get("SLOPERATOR_AGENT_MAX_CONCURRENCY", "2"))
        except ValueError as error:
            raise ConfigurationError(
                "Port, archive limits, and agent limits must be integers"
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
        if default_agent not in {"claude", "codex"}:
            raise ConfigurationError("SLOPERATOR_DEFAULT_AGENT must be claude or codex")
        if not claude_model or not codex_model:
            raise ConfigurationError("Agent model names must not be empty")
        if not 30 <= agent_timeout_seconds <= 86_400:
            raise ConfigurationError(
                "SLOPERATOR_AGENT_TIMEOUT_SECONDS must be between 30 and 86400"
            )
        if not 1 <= agent_max_concurrency <= 8:
            raise ConfigurationError("SLOPERATOR_AGENT_MAX_CONCURRENCY must be between 1 and 8")
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
            agent_workspace=agent_workspace,
            default_agent=default_agent,
            claude_model=claude_model,
            codex_model=codex_model,
            claude_cli=claude_cli,
            codex_cli=codex_cli,
            agent_timeout_seconds=agent_timeout_seconds,
            agent_max_concurrency=agent_max_concurrency,
        )
