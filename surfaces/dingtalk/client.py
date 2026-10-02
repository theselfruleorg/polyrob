"""Thin DingTalk Open-API client over aiohttp (the house thin-client style).

Auth is the app's access token (``POST /v1.0/oauth2/accessToken`` with the app's
Client ID / Client Secret, ~2 h lifetime): cached, refreshed five minutes before
it expires AND once when a call answers with a token-invalid code. Error text
never carries the secret or the token.

Two ways out:

* the **session webhook** an inbound event carries (``sessionWebhook`` +
  ``sessionWebhookExpiredTime``): the reply path for that conversation while it
  lives — a ``reply_window`` of kind ``session_webhook``. Only an ``https`` URL on
  a DingTalk host is used (the URL arrives inside an event);
* the **robot OpenAPI** otherwise (proactive): a DM goes to
  ``/v1.0/robot/oToMessages/batchSend`` with the user's staff id, a group to
  ``/v1.0/robot/groupMessages/send`` with its ``openConversationId``.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from core.surfaces import catalog as _catalog
from core.surfaces.surface import split_message

logger = logging.getLogger(__name__)

API_BASE = "https://api.dingtalk.com"

#: Answers that mean "your access token is missing/expired/invalid".
TOKEN_INVALID_CODES = frozenset({
    "InvalidAuthentication", "Forbidden.AccessDenied.AccessTokenInvalid",
    "InvalidAccessToken", "AccessTokenExpired",
})

#: Refresh this many seconds before the platform's stated expiry.
_REFRESH_MARGIN_S = 300

#: The per-message split, from the catalog row (the one place the limit lives).
MAX_TEXT_CHARS = _catalog.get("dingtalk").max_message_chars

#: Hosts a session webhook may point at (suffix match on a dot boundary).
SESSION_WEBHOOK_HOSTS = ("dingtalk.com",)

#: A session webhook is not used in its last few seconds (clock skew, latency).
_WEBHOOK_SAFETY_S = 30


def webhook_host_allowed(url: str) -> bool:
    parts = urlparse(url or "")
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == h or host.endswith("." + h) for h in SESSION_WEBHOOK_HOSTS)


@dataclass
class Conversation:
    """What the last inbound told us about one conversation."""
    webhook: str
    expires_at: float          # epoch seconds
    is_group: bool
    user_id: str               # the sender's staff id (a DM's proactive target)


class ConversationCache:
    """conversationId → the newest session webhook + how to reach it proactively.
    In memory: a restart simply falls back to the OpenAPI until the next inbound."""

    def __init__(self) -> None:
        self._rows: Dict[str, Conversation] = {}

    def remember(self, chat_id: str, conv: Conversation) -> None:
        if chat_id:
            self._rows[chat_id] = conv

    def get(self, chat_id: str) -> Optional[Conversation]:
        return self._rows.get(chat_id)

    def live_webhook(self, chat_id: str, now: Optional[float] = None) -> Optional[str]:
        conv = self._rows.get(chat_id)
        now = time.time() if now is None else now
        if conv and conv.webhook and now < conv.expires_at - _WEBHOOK_SAFETY_S \
                and webhook_host_allowed(conv.webhook):
            return conv.webhook
        return None


class DingTalkClient:
    def __init__(self, client_id: str, client_secret: str, *,
                 conversations: Optional[ConversationCache] = None) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self.conversations = conversations or ConversationCache()
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._session = None

    @property
    def robot_code(self) -> str:
        """An internal-app robot's robotCode is the app's Client ID."""
        return self._client_id

    # --- transport ------------------------------------------------------------
    async def _request(self, method: str, url: str, *, json=None,
                       headers=None) -> Tuple[int, dict]:
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        async with self._session.request(method, url, json=json,
                                         headers=headers or {}) as resp:
            try:
                payload = await resp.json(content_type=None)
            except Exception:
                payload = None
            return resp.status, payload if isinstance(payload, dict) else {}

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # --- auth -----------------------------------------------------------------
    async def access_token(self, *, force: bool = False, stale: Optional[str] = None) -> str:
        """The cached token, or a fresh one. ``force`` refreshes unless another
        caller already replaced ``stale`` (one fetch per expiry, not one per send)."""
        async with self._token_lock:
            fresh = self._token and time.monotonic() < self._token_expires_at
            if fresh and (not force or self._token != stale):
                return self._token
            status, payload = await self._request(
                "POST", API_BASE + "/v1.0/oauth2/accessToken",
                json={"appKey": self._client_id, "appSecret": self._client_secret})
            token = str(payload.get("accessToken") or "")
            if not token:
                raise RuntimeError("dingtalk accessToken failed: " + _reason(payload, status))
            try:
                ttl = int(payload.get("expireIn") or 7200)
            except (TypeError, ValueError):
                ttl = 7200
            self._token = token
            self._token_expires_at = time.monotonic() + max(60, ttl - _REFRESH_MARGIN_S)
            return self._token

    async def call(self, method: str, path: str, *, json=None) -> dict:
        """An authenticated OpenAPI call; a token-invalid answer refreshes ONCE."""
        token = None
        for attempt in (0, 1):
            token = await self.access_token(force=attempt == 1, stale=token)
            status, payload = await self._request(
                method, API_BASE + path, json=json,
                headers={"x-acs-dingtalk-access-token": token})
            invalid = status == 401 or payload.get("code") in TOKEN_INVALID_CODES
            if invalid and attempt == 0:
                logger.info("dingtalk: access token refused (%s) — refreshing",
                            payload.get("code") or status)
                continue
            if status >= 400 or (payload.get("code") and payload.get("message")):
                raise RuntimeError(f"dingtalk {path} failed: " + _reason(payload, status))
            return payload
        return payload  # pragma: no cover — the loop always returns or raises

    # --- sends ----------------------------------------------------------------
    async def reply_via_webhook(self, webhook: str, text: str) -> None:
        """POST ``text`` to a session webhook (no token needed), chunked."""
        if not webhook_host_allowed(webhook):
            raise RuntimeError("dingtalk session webhook refused: not a DingTalk https host")
        for chunk in split_message(text or "", MAX_TEXT_CHARS):
            if not chunk:
                continue
            status, payload = await self._request(
                "POST", webhook, json={"msgtype": "text", "text": {"content": chunk}})
            errcode = payload.get("errcode")
            if status >= 400 or (errcode not in (None, 0)):
                raise RuntimeError("dingtalk session webhook failed: "
                                   f"errcode {errcode} HTTP {status}")

    async def send_proactive(self, target: str, text: str, *, is_group: bool) -> dict:
        """The robot OpenAPI: a group by ``openConversationId``, a DM by staff id."""
        last: dict = {}
        for chunk in split_message(text or "", MAX_TEXT_CHARS):
            if not chunk:
                continue
            param = _json.dumps({"content": chunk}, ensure_ascii=False)
            if is_group:
                last = await self.call("POST", "/v1.0/robot/groupMessages/send", json={
                    "robotCode": self.robot_code, "openConversationId": target,
                    "msgKey": "sampleText", "msgParam": param})
            else:
                last = await self.call("POST", "/v1.0/robot/oToMessages/batchSend", json={
                    "robotCode": self.robot_code, "userIds": [target],
                    "msgKey": "sampleText", "msgParam": param})
        return last

    async def send_text(self, target: str, text: str) -> dict:
        """Send to a conversation or a user: the live session webhook first, the
        robot OpenAPI otherwise. ``target`` is a ``conversationId`` (``cid…``) the
        cache knows, or a staff id (the owner fan-out's ``OWNER_DINGTALK_ID``)."""
        target = str(target or "")
        webhook = self.conversations.live_webhook(target)
        if webhook:
            await self.reply_via_webhook(webhook, text)
            return {"via": "session_webhook"}
        conv = self.conversations.get(target)
        if conv is not None:
            if conv.is_group:
                return await self.send_proactive(target, text, is_group=True)
            return await self.send_proactive(conv.user_id, text, is_group=False)
        # Unknown to the cache: a conversation id names a group chat (a DM's
        # conversation id cannot be reached without its user); else a staff id.
        return await self.send_proactive(target, text, is_group=target.startswith("cid"))


def _reason(payload: dict, status) -> str:
    code = payload.get("code") if isinstance(payload, dict) else None
    if code:
        return f"code {code} {payload.get('message') or ''}".strip()
    return f"HTTP {status}"
