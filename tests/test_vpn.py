from pathlib import Path

from sloperator.config import Settings
from sloperator.vpn import VpnManager


def test_vpn_agent_environment_is_localhost_only(tmp_path: Path) -> None:
    manager = VpnManager(
        Settings(
            slack_user_id="U1234567890",
            bot_token="xoxb-test",
            app_token="xapp-test",
            database_path=tmp_path / "data.sqlite3",
            vpn_proxy_port=18888,
        )
    )

    environment = manager.agent_environment()

    assert environment["HTTPS_PROXY"] == "http://127.0.0.1:18888"
    assert environment["NO_PROXY"] == "127.0.0.1,localhost"


def test_vpn_runtime_credentials_preserve_special_characters(tmp_path: Path) -> None:
    profile = tmp_path / "client.ovpn"
    profile.write_text("client\n")
    manager = VpnManager(
        Settings(
            slack_user_id="U1234567890",
            bot_token="xoxb-test",
            app_token="xapp-test",
            database_path=tmp_path / "data" / "archive.sqlite3",
            ldap_username="user@mus.se",
            ldap_password=r"$pec!al\\password",
            vpn_profile=profile,
        )
    )

    manager._prepare_runtime_files()

    assert (manager.runtime_dir / "auth").read_text() == (
        "user@mus.se\n$pec!al\\\\password\n"
    )
