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


def _user_id_set(value: str, default: str) -> frozenset[str]:
    """Parse comma-separated Slack IDs, optionally wrapped in brackets."""
    normalized = value.strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    users = frozenset(
        item.strip().strip("'\"") for item in normalized.split(",") if item.strip().strip("'\"")
    )
    return users or frozenset({default})


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings loaded from the environment."""

    slack_user_id: str
    bot_token: str
    app_token: str
    slack_allowed_conversation_users: frozenset[str] = frozenset()
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
    slack_communication_model: str = "haiku"
    slack_communication_timeout_seconds: int = 120
    claude_cli: Path = Path("/home/egor/.local/bin/claude")
    codex_cli: Path = Path("/usr/bin/codex")
    agent_timeout_seconds: int = 2_700
    agent_max_concurrency: int = 2
    experiment_finalizer_enabled: bool = True
    experiment_finalizer_timezone: str = "Asia/Nicosia"
    experiment_finalizer_hour: int = 12
    experiment_finalizer_timeout_seconds: int = 7_200
    experiment_finalizer_channel: str = "C07A9FDQ14P"
    experiment_design_enabled: bool = True
    experiment_design_timezone: str = "Asia/Nicosia"
    experiment_design_hour: int = 15
    experiment_design_timeout_seconds: int = 7_200
    experiment_design_channel: str = "C07A9FDQ14P"
    experiment_analytics_enabled: bool = True
    experiment_analytics_timezone: str = "Asia/Nicosia"
    experiment_analytics_hour: int = 16
    experiment_analytics_timeout_seconds: int = 7_200
    experiment_analytics_channel: str = "C07A9FDQ14P"
    jira_url: str = "https://mu--se.atlassian.net"
    jira_username: str | None = None
    jira_api_token: str | None = None
    experiment_config_timeout_seconds: int = 7_200
    mobile_health_timeout_seconds: int = 3_600
    ldap_username: str | None = None
    ldap_password: str | None = None
    vpn_profile: Path = Path("/home/egor/hz config 2fa.ovpn")
    vpn_image: str = "local/openvpn-agent:24.04"
    vpn_container: str = "sloperator-vpn"
    vpn_proxy_port: int = 18_888
    anomaly_alert_channel: str = "C06FADPMGKT"
    anomaly_bot_user_id: str = "U018X57PTFV"
    anomaly_bot_id: str = "B018Q735LSJ"
    anomaly_window_hours: float = 24.0
    anomaly_threshold: float = 0.10
    anomaly_days_before: int = 1
    subscription_flow_alert_channel: str = "C06FADPMGKT"
    mobile_health_alert_channel: str = "C0AJKHFHVHV"
    mobile_health_bot_id: str = "B0AM51CS2H5"
    payment_layer_alert_channel: str = "C06FADPMGKT"
    clickhouse_host: str | None = None
    clickhouse_port: int = 8443
    clickhouse_username: str | None = None
    clickhouse_password: str = ""

    @property
    def conversation_user_ids(self) -> frozenset[str]:
        """Allowed channel-thread participants, falling back to the DM owner."""
        return self.slack_allowed_conversation_users or frozenset({self.slack_user_id})

    @classmethod
    def from_environment(cls) -> Settings:
        """Load a validated settings object, including a local ``.env`` file."""
        # Production systemd loads EnvironmentFile before making the secret
        # file inaccessible to this process and all spawned agent CLIs.
        with suppress(PermissionError):
            load_dotenv(interpolate=False)

        user_id = _required("SLACK_USER_ID")
        allowed_conversation_users = _user_id_set(
            os.environ.get("SLACK_ALLOWED_CONVERSATION_USERS", ""),
            user_id,
        )
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
        # The automated agents already use this repository-local environment. Loading it with
        # setdefault semantics lets the deterministic runtime use the same Jira read credentials.
        with suppress(PermissionError):
            load_dotenv(agent_workspace / ".env", override=False, interpolate=False)
        default_agent = os.environ.get("SLOPERATOR_DEFAULT_AGENT", "claude").strip().lower()
        claude_model = os.environ.get("SLOPERATOR_CLAUDE_MODEL", "opus").strip()
        codex_model = os.environ.get("SLOPERATOR_CODEX_MODEL", "gpt-5.6-sol").strip()
        slack_communication_model = os.environ.get(
            "SLOPERATOR_SLACK_COMMUNICATION_MODEL", "haiku"
        ).strip()
        slack_communication_timeout_seconds = int(
            os.environ.get("SLOPERATOR_SLACK_COMMUNICATION_TIMEOUT_SECONDS", "120")
        )
        claude_cli = Path(
            os.environ.get("SLOPERATOR_CLAUDE_CLI", "/home/egor/.local/bin/claude")
        ).expanduser()
        codex_cli = Path(os.environ.get("SLOPERATOR_CODEX_CLI", "/usr/bin/codex")).expanduser()
        experiment_finalizer_enabled = os.environ.get(
            "EXPERIMENT_FINALIZER_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        experiment_finalizer_timezone = os.environ.get(
            "EXPERIMENT_FINALIZER_TIMEZONE", "Asia/Nicosia"
        ).strip()
        experiment_finalizer_channel = os.environ.get(
            "EXPERIMENT_FINALIZER_CHANNEL", "C07A9FDQ14P"
        ).strip()
        experiment_design_enabled = os.environ.get(
            "EXPERIMENT_DESIGN_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        experiment_design_timezone = os.environ.get(
            "EXPERIMENT_DESIGN_TIMEZONE", "Asia/Nicosia"
        ).strip()
        experiment_design_channel = os.environ.get(
            "EXPERIMENT_DESIGN_CHANNEL", "C07A9FDQ14P"
        ).strip()
        experiment_analytics_enabled = os.environ.get(
            "EXPERIMENT_ANALYTICS_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        experiment_analytics_timezone = os.environ.get(
            "EXPERIMENT_ANALYTICS_TIMEZONE", "Asia/Nicosia"
        ).strip()
        experiment_analytics_channel = os.environ.get(
            "EXPERIMENT_ANALYTICS_CHANNEL", "C07A9FDQ14P"
        ).strip()
        jira_url = os.environ.get("JIRA_URL", "https://mu--se.atlassian.net").strip()
        jira_username = os.environ.get("JIRA_USERNAME", "").strip() or None
        jira_api_token = os.environ.get("JIRA_API_TOKEN", "").strip() or None
        mobile_health_timeout_seconds = int(os.environ.get("MOBILE_HEALTH_TIMEOUT_SECONDS", "3600"))
        ldap_username = os.environ.get("LDAP_USERNAME")
        ldap_password = os.environ.get("LDAP_PASSWORD")
        vpn_profile = Path(
            os.environ.get("SLOPERATOR_VPN_PROFILE", "/home/egor/hz config 2fa.ovpn")
        ).expanduser()
        vpn_image = os.environ.get("SLOPERATOR_VPN_IMAGE", "local/openvpn-agent:24.04").strip()
        vpn_container = os.environ.get("SLOPERATOR_VPN_CONTAINER", "sloperator-vpn").strip()
        anomaly_alert_channel = os.environ.get("ANOMALY_ALERT_CHANNEL", "C06FADPMGKT").strip()
        anomaly_bot_user_id = os.environ.get("ANOMALY_BOT_USER_ID", "U018X57PTFV").strip()
        anomaly_bot_id = os.environ.get("ANOMALY_BOT_ID", "B018Q735LSJ").strip()
        subscription_flow_alert_channel = os.environ.get(
            "SUBFLOW_ALERT_CHANNEL", "C06FADPMGKT"
        ).strip()
        mobile_health_alert_channel = os.environ.get(
            "MOBILE_HEALTH_ALERT_CHANNEL", "C0AJKHFHVHV"
        ).strip()
        mobile_health_bot_id = os.environ.get("MOBILE_HEALTH_BOT_ID", "B0AM51CS2H5").strip()
        payment_layer_alert_channel = os.environ.get(
            "PAYMENT_MONITOR_ALERT_CHANNEL", "C06FADPMGKT"
        ).strip()
        clickhouse_host = os.environ.get("CLICKHOUSE_HOST", "").strip() or None
        clickhouse_username = os.environ.get("CLICKHOUSE_USERNAME", "").strip() or None
        clickhouse_password = os.environ.get("CLICKHOUSE_PASSWORD", "")

        try:
            port = int(os.environ.get("SLOPERATOR_PORT", "8080"))
            backfill_limit = int(os.environ.get("SLOPERATOR_BACKFILL_LIMIT", "100"))
            sync_interval_seconds = int(os.environ.get("SLOPERATOR_SYNC_INTERVAL_SECONDS", "300"))
            agent_timeout_seconds = int(os.environ.get("SLOPERATOR_AGENT_TIMEOUT_SECONDS", "2700"))
            agent_max_concurrency = int(os.environ.get("SLOPERATOR_AGENT_MAX_CONCURRENCY", "2"))
            experiment_finalizer_hour = int(os.environ.get("EXPERIMENT_FINALIZER_HOUR", "12"))
            experiment_finalizer_timeout_seconds = int(
                os.environ.get("EXPERIMENT_FINALIZER_TIMEOUT_SECONDS", "7200")
            )
            experiment_design_hour = int(os.environ.get("EXPERIMENT_DESIGN_HOUR", "15"))
            experiment_design_timeout_seconds = int(
                os.environ.get("EXPERIMENT_DESIGN_TIMEOUT_SECONDS", "7200")
            )
            experiment_analytics_hour = int(
                os.environ.get("EXPERIMENT_ANALYTICS_HOUR", "16")
            )
            experiment_analytics_timeout_seconds = int(
                os.environ.get("EXPERIMENT_ANALYTICS_TIMEOUT_SECONDS", "7200")
            )
            experiment_config_timeout_seconds = int(
                os.environ.get("EXPERIMENT_CONFIG_TIMEOUT_SECONDS", "7200")
            )
            vpn_proxy_port = int(os.environ.get("SLOPERATOR_VPN_PROXY_PORT", "18888"))
            anomaly_window_hours = float(os.environ.get("ANOMALY_WINDOW_HOURS", "24"))
            anomaly_threshold = float(os.environ.get("ANOMALY_THRESHOLD", "0.10"))
            anomaly_days_before = int(float(os.environ.get("ANOMALY_DAYS_BEFORE", "1")))
            clickhouse_port = int(os.environ.get("CLICKHOUSE_PORT", "8443"))
        except ValueError as error:
            raise ConfigurationError(
                "Port, archive limits, and agent limits must be integers"
            ) from error

        if not user_id.startswith("U"):
            raise ConfigurationError("SLACK_USER_ID must be a Slack user ID")
        if any(not user.startswith("U") for user in allowed_conversation_users):
            raise ConfigurationError("SLACK_ALLOWED_CONVERSATION_USERS must contain Slack user IDs")
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
        if not 0 <= experiment_finalizer_hour <= 23:
            raise ConfigurationError("EXPERIMENT_FINALIZER_HOUR must be between 0 and 23")
        if not 300 <= experiment_finalizer_timeout_seconds <= 86_400:
            raise ConfigurationError(
                "EXPERIMENT_FINALIZER_TIMEOUT_SECONDS must be between 300 and 86400"
            )
        if not 300 <= experiment_config_timeout_seconds <= 86_400:
            raise ConfigurationError(
                "EXPERIMENT_CONFIG_TIMEOUT_SECONDS must be between 300 and 86400"
            )
        if not 300 <= mobile_health_timeout_seconds <= 86_400:
            raise ConfigurationError("MOBILE_HEALTH_TIMEOUT_SECONDS must be between 300 and 86400")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(experiment_finalizer_timezone)
        except (KeyError, ValueError) as error:
            raise ConfigurationError(
                "EXPERIMENT_FINALIZER_TIMEZONE must be a valid IANA timezone"
            ) from error
        if not experiment_finalizer_channel.startswith("C"):
            raise ConfigurationError("EXPERIMENT_FINALIZER_CHANNEL must be a Slack channel ID")
        if not 0 <= experiment_design_hour <= 23:
            raise ConfigurationError("EXPERIMENT_DESIGN_HOUR must be between 0 and 23")
        if not 300 <= experiment_design_timeout_seconds <= 86_400:
            raise ConfigurationError(
                "EXPERIMENT_DESIGN_TIMEOUT_SECONDS must be between 300 and 86400"
            )
        try:
            ZoneInfo(experiment_design_timezone)
        except (KeyError, ValueError) as error:
            raise ConfigurationError(
                "EXPERIMENT_DESIGN_TIMEZONE must be a valid IANA timezone"
            ) from error
        if not experiment_design_channel.startswith("C"):
            raise ConfigurationError("EXPERIMENT_DESIGN_CHANNEL must be a Slack channel ID")
        if not 0 <= experiment_analytics_hour <= 23:
            raise ConfigurationError("EXPERIMENT_ANALYTICS_HOUR must be between 0 and 23")
        if not 300 <= experiment_analytics_timeout_seconds <= 86_400:
            raise ConfigurationError(
                "EXPERIMENT_ANALYTICS_TIMEOUT_SECONDS must be between 300 and 86400"
            )
        try:
            ZoneInfo(experiment_analytics_timezone)
        except (KeyError, ValueError) as error:
            raise ConfigurationError(
                "EXPERIMENT_ANALYTICS_TIMEZONE must be a valid IANA timezone"
            ) from error
        if not experiment_analytics_channel.startswith("C"):
            raise ConfigurationError("EXPERIMENT_ANALYTICS_CHANNEL must be a Slack channel ID")
        if not jira_url.startswith("https://"):
            raise ConfigurationError("JIRA_URL must be an HTTPS URL")
        if bool(jira_username) != bool(jira_api_token):
            raise ConfigurationError("JIRA_USERNAME and JIRA_API_TOKEN must be set together")
        if not 1 <= vpn_proxy_port <= 65_535:
            raise ConfigurationError("SLOPERATOR_VPN_PROXY_PORT must be between 1 and 65535")
        if not anomaly_alert_channel.startswith("C"):
            raise ConfigurationError("ANOMALY_ALERT_CHANNEL must be a Slack channel ID")
        if not anomaly_bot_user_id.startswith("U"):
            raise ConfigurationError("ANOMALY_BOT_USER_ID must be a Slack user ID")
        if anomaly_bot_id and not anomaly_bot_id.startswith("B"):
            raise ConfigurationError("ANOMALY_BOT_ID must be a Slack bot ID")
        if anomaly_window_hours <= 0:
            raise ConfigurationError("ANOMALY_WINDOW_HOURS must be positive")
        if not 0 < anomaly_threshold < 1:
            raise ConfigurationError("ANOMALY_THRESHOLD must be between 0 and 1")
        if anomaly_days_before < 1:
            raise ConfigurationError("ANOMALY_DAYS_BEFORE must be positive")
        if not subscription_flow_alert_channel.startswith("C"):
            raise ConfigurationError("SUBFLOW_ALERT_CHANNEL must be a Slack channel ID")
        if not mobile_health_alert_channel.startswith("C"):
            raise ConfigurationError("MOBILE_HEALTH_ALERT_CHANNEL must be a Slack channel ID")
        if not mobile_health_bot_id.startswith("B"):
            raise ConfigurationError("MOBILE_HEALTH_BOT_ID must be a Slack bot ID")
        if not payment_layer_alert_channel.startswith("C"):
            raise ConfigurationError("PAYMENT_MONITOR_ALERT_CHANNEL must be a Slack channel ID")
        if not 1 <= clickhouse_port <= 65_535:
            raise ConfigurationError("CLICKHOUSE_PORT must be between 1 and 65535")
        if bool(clickhouse_host) != bool(clickhouse_username):
            raise ConfigurationError("CLICKHOUSE_HOST and CLICKHOUSE_USERNAME must be set together")
        if bool(ldap_username) != bool(ldap_password):
            raise ConfigurationError("LDAP_USERNAME and LDAP_PASSWORD must be set together")
        if ldap_username and any(character in ldap_username for character in "\r\n"):
            raise ConfigurationError("LDAP_USERNAME must not contain line breaks")
        if ldap_password and any(character in ldap_password for character in "\r\n"):
            raise ConfigurationError("LDAP_PASSWORD must not contain line breaks")
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("SLOPERATOR_LOG_LEVEL is invalid")
        if not slack_communication_model:
            raise ConfigurationError("SLOPERATOR_SLACK_COMMUNICATION_MODEL must not be empty")
        if slack_communication_timeout_seconds <= 0:
            raise ConfigurationError(
                "SLOPERATOR_SLACK_COMMUNICATION_TIMEOUT_SECONDS must be positive"
            )

        return cls(
            slack_user_id=user_id,
            bot_token=bot_token,
            app_token=app_token,
            slack_allowed_conversation_users=allowed_conversation_users,
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
            slack_communication_model=slack_communication_model,
            slack_communication_timeout_seconds=slack_communication_timeout_seconds,
            claude_cli=claude_cli,
            codex_cli=codex_cli,
            agent_timeout_seconds=agent_timeout_seconds,
            agent_max_concurrency=agent_max_concurrency,
            experiment_finalizer_enabled=experiment_finalizer_enabled,
            experiment_finalizer_timezone=experiment_finalizer_timezone,
            experiment_finalizer_hour=experiment_finalizer_hour,
            experiment_finalizer_timeout_seconds=experiment_finalizer_timeout_seconds,
            experiment_finalizer_channel=experiment_finalizer_channel,
            experiment_design_enabled=experiment_design_enabled,
            experiment_design_timezone=experiment_design_timezone,
            experiment_design_hour=experiment_design_hour,
            experiment_design_timeout_seconds=experiment_design_timeout_seconds,
            experiment_design_channel=experiment_design_channel,
            experiment_analytics_enabled=experiment_analytics_enabled,
            experiment_analytics_timezone=experiment_analytics_timezone,
            experiment_analytics_hour=experiment_analytics_hour,
            experiment_analytics_timeout_seconds=experiment_analytics_timeout_seconds,
            experiment_analytics_channel=experiment_analytics_channel,
            jira_url=jira_url,
            jira_username=jira_username,
            jira_api_token=jira_api_token,
            experiment_config_timeout_seconds=experiment_config_timeout_seconds,
            mobile_health_timeout_seconds=mobile_health_timeout_seconds,
            ldap_username=ldap_username,
            ldap_password=ldap_password,
            vpn_profile=vpn_profile,
            vpn_image=vpn_image,
            vpn_container=vpn_container,
            vpn_proxy_port=vpn_proxy_port,
            anomaly_alert_channel=anomaly_alert_channel,
            anomaly_bot_user_id=anomaly_bot_user_id,
            anomaly_bot_id=anomaly_bot_id,
            anomaly_window_hours=anomaly_window_hours,
            anomaly_threshold=anomaly_threshold,
            anomaly_days_before=anomaly_days_before,
            subscription_flow_alert_channel=subscription_flow_alert_channel,
            mobile_health_alert_channel=mobile_health_alert_channel,
            mobile_health_bot_id=mobile_health_bot_id,
            payment_layer_alert_channel=payment_layer_alert_channel,
            clickhouse_host=clickhouse_host,
            clickhouse_port=clickhouse_port,
            clickhouse_username=clickhouse_username,
            clickhouse_password=clickhouse_password,
        )
