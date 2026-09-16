"""Observe delivery at the shared Slack API boundary, including direct scheduler posts."""

from __future__ import annotations

from typing import Any

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from sloperator.operations_store import emit_runtime
from sloperator.slack_identity import resolve_payload_mentions


class ObservedSlackClient(AsyncWebClient):
    async def api_call(self, api_method: str, **kwargs: Any) -> AsyncSlackResponse:
        payload = kwargs.get("json") or kwargs.get("data") or {}
        if not isinstance(payload, dict):
            payload = {}
        if api_method in {"chat.postMessage", "chat.update"}:
            payload = await resolve_payload_mentions(self, payload)
            if "json" in kwargs:
                kwargs["json"] = payload
            elif "data" in kwargs:
                kwargs["data"] = payload
        channel = str(payload.get("channel") or payload.get("channel_id") or "")
        thread = str(payload.get("thread_ts") or "")
        try:
            result = await super().api_call(api_method, **kwargs)
        except Exception as error:
            emit_runtime(
                "Slack API " + api_method,
                "failed",
                f"{type(error).__name__}: {error}",
                f"{channel}/{thread}",
            )
            raise
        if api_method in {"chat.postMessage", "chat.update", "files.completeUploadExternal"}:
            channel = str(result.get("channel") or channel)
            thread = thread or str(result.get("ts") or "")
            detail = str(payload.get("markdown_text") or payload.get("text") or "Файл отправлен")
            if channel.startswith(("D", "G", "U")):
                detail = "Личное сообщение/вложение доставлено; содержимое в приватном логе сессии."
            emit_runtime("Slack " + api_method, "delivered", detail, f"{channel}/{thread}")
        return result


class ObserveRequestClient:
    """Bolt creates a base SDK client per event; instrument that client as well."""

    async def __call__(self, context: Any, next: Any) -> Any:
        original = context.client
        if not isinstance(original, ObservedSlackClient):
            context["client"] = ObservedSlackClient(
                token=original.token,
                base_url=original.base_url,
                timeout=original.timeout,
                ssl=original.ssl,
                proxy=original.proxy,
                session=original.session,
                trust_env_in_session=original.trust_env_in_session,
                headers=original.headers.copy(),
                team_id=original.default_params.get("team_id"),
                logger=original.logger,
                retry_handlers=original.retry_handlers.copy(),
            )
        return await next()
