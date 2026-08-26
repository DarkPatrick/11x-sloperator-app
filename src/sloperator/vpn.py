"""Isolated OpenVPN lifecycle with Keycloak LDAP and OTP authentication."""

from __future__ import annotations

import asyncio
import os
import re
from email.message import Message
from enum import StrEnum
from html.parser import HTMLParser
from http.cookiejar import MozillaCookieJar
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, OpenerDirector, Request, build_opener

from sloperator.config import Settings

WEB_AUTH_RE = re.compile(r"WEB_AUTH::(https://\S+)")
OTP_RE = re.compile(r"^\d{6,8}$")


class VpnError(RuntimeError):
    """Raised when the managed VPN cannot complete an operation."""


class VpnState(StrEnum):
    """Externally relevant states of the managed connection."""

    STOPPED = "stopped"
    CONNECTING = "connecting"
    WAITING_OTP = "waiting_otp"
    CONNECTED = "connected"
    FAILED = "failed"


class _LoginPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.inputs: dict[str, str] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = attributes.get("action")
        if tag == "input" and (name := attributes.get("name")):
            self.inputs[name] = attributes.get("value") or ""


class _HttpResponse(Protocol):
    headers: Message
    url: str

    def read(self) -> bytes: ...


def _read_response(response: _HttpResponse) -> tuple[str, str]:
    charset = response.headers.get_content_charset() or "utf-8"
    return response.read().decode(charset, errors="replace"), str(response.url)


class VpnManager:
    """Manage one resource-limited VPN/proxy container."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runtime_dir = settings.database_path.expanduser().resolve().parent / "vpn-runtime"
        self.cookie_path = self.runtime_dir / "webauth.cookies"
        self.otp_page_path = self.runtime_dir / "otp.html"
        self._lock = asyncio.Lock()

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.vpn_proxy_port}"

    @property
    def configured(self) -> bool:
        return bool(self.settings.ldap_username and self.settings.ldap_password)

    async def connect(self) -> VpnState:
        """Start a fresh VPN handshake and complete its LDAP phase."""
        async with self._lock:
            if await self.state() is VpnState.CONNECTED:
                return VpnState.CONNECTED
            self._validate()
            self._prepare_runtime_files()
            await self._remove_container()
            await self._docker(
                "run",
                "-d",
                "--name",
                self.settings.vpn_container,
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_ADMIN",
                "--cap-add",
                "DAC_OVERRIDE",
                "--device",
                "/dev/net/tun:/dev/net/tun",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "128m",
                "--pids-limit",
                "64",
                "-p",
                f"127.0.0.1:{self.settings.vpn_proxy_port}:8888",
                "-v",
                f"{self.runtime_dir / 'client.ovpn'}:/vpn/client.ovpn:ro",
                "-v",
                f"{self.runtime_dir / 'auth'}:/run/secrets/openvpn-auth:ro",
                self.settings.vpn_image,
                "--config",
                "/vpn/client.ovpn",
                "--auth-user-pass",
                "/run/secrets/openvpn-auth",
                "--auth-nocache",
                "--connect-retry-max",
                "1",
                "--connect-timeout",
                "15",
            )
            auth_url = await self._wait_for_auth_url()
            state = await asyncio.to_thread(self._submit_ldap, auth_url)
            if state is VpnState.CONNECTED:
                state = await self._wait_for_connection()
            if state is VpnState.CONNECTED:
                self._clear_transient_secrets()
            return state

    async def submit_otp(self, code: str) -> VpnState:
        """Submit an OTP to the currently pending Keycloak session."""
        if not OTP_RE.fullmatch(code):
            raise VpnError("Одноразовый код должен содержать 6-8 цифр.")
        async with self._lock:
            if not self.cookie_path.is_file() or not self.otp_page_path.is_file():
                raise VpnError("Сейчас нет VPN-сессии, ожидающей одноразовый код.")
            state = await asyncio.to_thread(self._submit_otp, code)
            if state is VpnState.WAITING_OTP:
                raise VpnError("Код не принят или уже истёк. Пришлите новый код.")
            state = await self._wait_for_connection()
            if state is VpnState.CONNECTED:
                self._clear_transient_secrets()
            return state

    async def state(self) -> VpnState:
        """Inspect Docker and OpenVPN without changing them."""
        result = await self._docker(
            "inspect",
            self.settings.vpn_container,
            "--format",
            "{{.State.Running}}",
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            return VpnState.STOPPED
        logs = (await self._docker("logs", self.settings.vpn_container, check=False)).combined
        if "Initialization Sequence Completed" in logs:
            return VpnState.CONNECTED
        if self.cookie_path.is_file() and self.otp_page_path.is_file():
            return VpnState.WAITING_OTP
        if "AUTH_FAILED" in logs or "fatal error" in logs:
            return VpnState.FAILED
        return VpnState.CONNECTING

    async def stop(self) -> None:
        """Stop the managed container and delete transient login material."""
        async with self._lock:
            await self._remove_container()
            self._clear_transient_secrets()

    def agent_environment(self) -> dict[str, str]:
        """Return proxy variables inherited by agent CLIs and their tools."""
        return {
            "HTTP_PROXY": self.proxy_url,
            "HTTPS_PROXY": self.proxy_url,
            "http_proxy": self.proxy_url,
            "https_proxy": self.proxy_url,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    def _validate(self) -> None:
        if not self.configured:
            raise VpnError("LDAP_USERNAME и LDAP_PASSWORD не настроены.")
        if not self.settings.vpn_profile.is_file():
            raise VpnError(f"VPN-конфиг не найден: {self.settings.vpn_profile}")

    def _prepare_runtime_files(self) -> None:
        username = self.settings.ldap_username
        password = self.settings.ldap_password
        if username is None or password is None:
            raise VpnError("LDAP credentials are unavailable")
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.runtime_dir, 0o700)
        profile_path = self.runtime_dir / "client.ovpn"
        auth_path = self.runtime_dir / "auth"
        # Docker creates a directory at a missing bind-mount source. This can
        # happen after transient credentials are removed while the stopped
        # container still exists, and would otherwise make reconnect fail.
        if auth_path.is_dir():
            try:
                auth_path.rmdir()
            except OSError as error:
                raise VpnError(
                    f"VPN auth path is an unexpected non-empty directory: {auth_path}"
                ) from error
        profile_path.write_bytes(self.settings.vpn_profile.read_bytes())
        auth_path.write_text(f"{username}\n{password}\n")
        os.chmod(profile_path, 0o600)
        os.chmod(auth_path, 0o600)

    async def _wait_for_auth_url(self) -> str:
        for _ in range(30):
            logs = (await self._docker("logs", self.settings.vpn_container, check=False)).combined
            if match := WEB_AUTH_RE.search(logs):
                return match.group(1).rstrip("')\".,")
            if "AUTH_FAILED" in logs or "fatal error" in logs:
                raise VpnError("OpenVPN отклонил подключение до web-авторизации.")
            await asyncio.sleep(1)
        raise VpnError("OpenVPN не выдал ссылку web-авторизации за 30 секунд.")

    async def _wait_for_connection(self) -> VpnState:
        for _ in range(60):
            state = await self.state()
            if state in {VpnState.CONNECTED, VpnState.FAILED, VpnState.STOPPED}:
                return state
            await asyncio.sleep(1)
        raise VpnError("VPN не завершил подключение за 60 секунд.")

    def _submit_ldap(self, auth_url: str) -> VpnState:
        jar = MozillaCookieJar(str(self.cookie_path))
        opener = build_opener(HTTPCookieProcessor(jar))
        response = opener.open(auth_url, timeout=20)
        body, _ = _read_response(response)
        page = _parse_page(body)
        if page.action is None:
            raise VpnError("Keycloak не вернул LDAP login form.")
        payload = {
            key: value
            for key, value in page.inputs.items()
            if key not in {"username", "password", "login"}
        }
        payload.update(
            username=self.settings.ldap_username or "",
            password=self.settings.ldap_password or "",
            login="Sign In",
        )
        body, final_url = _post_form(opener, page.action, payload)
        result = _parse_page(body)
        if "otp" in result.inputs and result.action:
            self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            jar.save(ignore_discard=True, ignore_expires=True)
            self.otp_page_path.write_text(body)
            os.chmod(self.cookie_path, 0o600)
            os.chmod(self.otp_page_path, 0o600)
            return VpnState.WAITING_OTP
        return _callback_state(final_url)

    def _submit_otp(self, code: str) -> VpnState:
        page = _parse_page(self.otp_page_path.read_text(errors="replace"))
        if page.action is None:
            raise VpnError("Сохранённая OTP form повреждена.")
        jar = MozillaCookieJar(str(self.cookie_path))
        jar.load(ignore_discard=True, ignore_expires=True)
        opener = build_opener(HTTPCookieProcessor(jar))
        payload = {
            key: value
            for key, value in page.inputs.items()
            if key not in {"otp", "login"}
        }
        payload.update(otp=code, login="Sign In")
        body, final_url = _post_form(opener, page.action, payload)
        result = _parse_page(body)
        if "otp" in result.inputs and result.action:
            jar.save(ignore_discard=True, ignore_expires=True)
            self.otp_page_path.write_text(body)
            os.chmod(self.cookie_path, 0o600)
            os.chmod(self.otp_page_path, 0o600)
            return VpnState.WAITING_OTP
        return _callback_state(final_url)

    async def _remove_container(self) -> None:
        await self._docker("rm", "-f", self.settings.vpn_container, check=False)

    def _clear_transient_secrets(self) -> None:
        for path in (
            self.runtime_dir / "auth",
            self.cookie_path,
            self.otp_page_path,
        ):
            path.unlink(missing_ok=True)

    async def _docker(
        self,
        *arguments: str,
        check: bool = True,
    ) -> _CommandResult:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/docker",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        result = _CommandResult(
            process.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
        if check and result.returncode != 0:
            raise VpnError(f"Docker command failed: {result.stderr[-1_000:]}")
        return result


class _CommandResult:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def combined(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


def _parse_page(body: str) -> _LoginPage:
    page = _LoginPage()
    page.feed(body)
    return page


def _post_form(
    opener: OpenerDirector,
    action: str,
    payload: dict[str, str],
) -> tuple[str, str]:
    request = Request(
        action,
        data=urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        response = opener.open(request, timeout=30)
    except HTTPError as error:
        response = error
    return _read_response(response)


def _callback_state(final_url: str) -> VpnState:
    parsed = urlparse(final_url)
    if parsed.netloc == "ovpn.mu.se" and parsed.path.startswith("/oauth2/callback"):
        return VpnState.CONNECTED
    raise VpnError("Keycloak не завершил OpenVPN callback.")
