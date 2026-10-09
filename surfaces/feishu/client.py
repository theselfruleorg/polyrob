"""Thin Feishu / Lark Open-API client over aiohttp (the house thin-client style).

Auth is the app's ``tenant_access_token`` (``auth/v3/tenant_access_token/internal``,
2 h lifetime): cached, refreshed five minutes before it expires AND once when a
call answers with a token-invalid code. The Open API answers HTTP 200/400 with
``{"code": <int>, "msg": ...}``; a non-zero code is raised as RuntimeError whose
text never carries the secret or the token.

``domain`` is ``lark`` (open.larksuite.com, international — the DEFAULT: it
needs no mainland-verified entity) or ``feishu`` (open.feishu.cn, mainland) —
the same app model on two hosts.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import re
import time
from typing import Optional

from core.surfaces import catalog as _catalog
from core.surfaces.surface import split_message

logger = logging.getLogger(__name__)


def _literal_mentions(value):
    if isinstance(value, str):
        return re.sub(r'<\s*/?\s*at\b', lambda m: '‹' + m.group(0)[1:], value, flags=re.I)
    if isinstance(value, list):
        return [_literal_mentions(item) for item in value]
    if isinstance(value, dict):
        if value.get('tag') == 'at':
            raise ValueError('Structured mentions are not permitted in generated cards')
        return {key: _literal_mentions(item) for key, item in value.items()}
    return value

_HOSTS = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}

#: The Open-API codes that mean "your tenant token is missing/expired/invalid".
TOKEN_INVALID_CODES = frozenset({99991661, 99991663})

#: Refresh this many seconds before the platform's stated expiry.
_REFRESH_MARGIN_S = 300

#: The platform's upload limits (im/v1/images: 10 MB, im/v1/files: 30 MB).
IMAGE_MAX_BYTES = 10 * 1024 * 1024
FILE_MAX_BYTES = 30 * 1024 * 1024

#: The per-message split, from the catalog row (the one place the limit lives).
MAX_TEXT_CHARS = _catalog.get("feishu").max_message_chars


def api_base(domain: Optional[str]) -> str:
    return _HOSTS.get((domain or "lark").strip().lower(), _HOSTS["lark"])


def receive_id_type(target: str) -> str:
    """The ``receive_id_type`` an address needs: chats are ``oc_``, a user's
    app-scoped id ``ou_``, a tenant-wide union id ``on_``; else email / user_id."""
    t = str(target or "")
    if t.startswith("oc_"):
        return "chat_id"
    if t.startswith("ou_"):
        return "open_id"
    if t.startswith("on_"):
        return "union_id"
    if "@" in t:
        return "email"
    return "user_id"


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, *, domain: str = "lark") -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self.domain = "feishu" if (domain or "").strip().lower() == "feishu" else "lark"
        self.base = api_base(self.domain)
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._session = None

    # --- transport ------------------------------------------------------------
    async def _request(self, method: str, path: str, *, json=None, headers=None,
                       params=None, data=None) -> dict:
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        async with self._session.request(method, self.base + path, json=json, data=data,
                                         headers=headers or {}, params=params) as resp:
            payload = await resp.json(content_type=None)
        return payload if isinstance(payload, dict) else {"code": -1, "msg": f"HTTP {resp.status}"}

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # --- auth -----------------------------------------------------------------
    async def tenant_token(self, *, force: bool = False, stale: Optional[str] = None) -> str:
        """The cached token, or a fresh one. ``force`` refreshes unless another
        caller already replaced ``stale`` (one fetch per expiry, not one per send)."""
        async with self._token_lock:
            fresh = self._token and time.monotonic() < self._token_expires_at
            if fresh and (not force or self._token != stale):
                return self._token
            payload = await self._request(
                "POST", "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret})
            self._check("tenant_access_token", payload)
            token = str(payload.get("tenant_access_token") or "")
            if not token:
                raise RuntimeError("feishu tenant_access_token failed: no token in the answer")
            try:
                ttl = int(payload.get("expire") or 7200)
            except (TypeError, ValueError):
                ttl = 7200
            self._token = token
            self._token_expires_at = time.monotonic() + max(60, ttl - _REFRESH_MARGIN_S)
            return self._token

    async def call(self, method: str, path: str, *, json=None, params=None,
                   form=None) -> dict:
        """An authenticated call; a token-invalid answer refreshes ONCE and retries.
        ``form`` is a zero-argument factory of a multipart body (a body is
        consumed by a send, so the retry builds a fresh one)."""
        token = None
        for attempt in (0, 1):
            token = await self.tenant_token(force=attempt == 1, stale=token)
            extra = {"data": form()} if form is not None else {}
            payload = await self._request(method, path, json=json, params=params,
                                          headers={"Authorization": f"Bearer {token}"},
                                          **extra)
            if payload.get("code") in TOKEN_INVALID_CODES and attempt == 0:
                logger.info("feishu: tenant token refused (%s) — refreshing", payload.get("code"))
                continue
            self._check(path, payload)
            return payload
        return payload  # pragma: no cover — the loop always returns or raises

    @staticmethod
    def _check(what: str, payload: dict) -> None:
        code = payload.get("code")
        if code == 0:
            return
        raise RuntimeError(f"feishu {what} failed: code {code} {payload.get('msg') or ''}".strip())

    # --- API ------------------------------------------------------------------
    async def bot_info(self) -> dict:
        """``bot/v3/info`` → ``{"app_name", "open_id", ...}`` (the bot's own id)."""
        payload = await self.call("GET", "/open-apis/bot/v3/info")
        return payload.get("bot") or {}

    # --- media (order 0004) ------------------------------------------------------
    def resource_url(self, message_id: str, file_key: str, kind: str) -> str:
        """Where an inbound attachment's bytes live (``type`` is ``image`` or ``file``)."""
        from urllib.parse import quote
        rtype = "image" if kind == "image" else "file"
        return (f"{self.base}/open-apis/im/v1/messages/{quote(message_id, safe='')}"
                f"/resources/{quote(file_key, safe='')}?type={rtype}")

    @property
    def host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.base).hostname or ""

    async def auth_header(self) -> dict:
        return {"Authorization": f"Bearer {await self.tenant_token()}"}

    async def upload(self, path: str, *, image: bool) -> str:
        """Upload a local file; the ``image_key`` / ``file_key`` to send it with."""
        import os
        import aiohttp
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            blob = fh.read()
        limit = IMAGE_MAX_BYTES if image else FILE_MAX_BYTES
        if len(blob) > limit:
            raise RuntimeError(f"{name} is {len(blob)} bytes, over the {limit}-byte limit")
        if not blob:
            raise RuntimeError(f"{name} is empty")

        def _form():
            form = aiohttp.FormData()
            if image:
                form.add_field("image_type", "message")
                form.add_field("image", blob, filename=name)
            else:
                form.add_field("file_type", "stream")
                form.add_field("file_name", name)
                form.add_field("file", blob, filename=name)
            return form

        payload = await self.call("POST", "/open-apis/im/v1/images" if image
                                  else "/open-apis/im/v1/files", form=_form)
        data = payload.get("data") or {}
        key = data.get("image_key" if image else "file_key")
        if not key:
            raise RuntimeError(f"feishu upload of {name} returned no key")
        return str(key)

    async def send_media(self, target: str, path: str, *, image: bool) -> dict:
        key = await self.upload(path, image=image)
        content = {"image_key": key} if image else {"file_key": key}
        payload = await self.call(
            "POST", "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type(target)},
            json={"receive_id": str(target), "msg_type": "image" if image else "file",
                  "content": _json.dumps(content)})
        return payload.get("data") or {}

    async def send_card(self, target: str, card: dict) -> dict:
        """Order 0005: one interactive card (the action buttons)."""
        payload = await self.call(
            "POST", "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type(target)},
            json={"receive_id": str(target), "msg_type": "interactive",
                  "content": _json.dumps(_literal_mentions(card), ensure_ascii=False)})
        return payload.get("data") or {}

    async def send_message(self, target: str, text: str) -> dict:
        """Send ``text`` to a chat or a user, split at :data:`MAX_TEXT_CHARS`.
        Returns the last message's ``data`` (``message_id``)."""
        text = _literal_mentions(text or '')
        last: dict = {}
        for chunk in split_message(text or "", MAX_TEXT_CHARS):
            if not chunk:
                continue
            payload = await self.call(
                "POST", "/open-apis/im/v1/messages",
                params={"receive_id_type": receive_id_type(target)},
                json={"receive_id": str(target), "msg_type": "text",
                      "content": _json.dumps({"text": chunk}, ensure_ascii=False)})
            last = payload.get("data") or {}
        return last
