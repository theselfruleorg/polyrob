"""Thin Slack Web-API client over aiohttp.

Bot token (``xoxb-``) for chat/auth calls; app-level token (``xapp-``) for
``apps.connections.open`` (Socket Mode WS URL). Slack Web API returns 200 with
``{"ok": false, "error": ...}`` on failure — raised as RuntimeError.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_API = "https://slack.com/api"


class SlackClient:
    def __init__(self, bot_token: Optional[str] = None,
                 app_token: Optional[str] = None) -> None:
        self._bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN", "")
        self._app_token = app_token or os.getenv("SLACK_APP_TOKEN", "")
        self._session = None
        self._dm_channel_cache: dict = {}  # user id -> D… conversation id

    async def _http(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _call(self, method: str, *, token: str,
                    json: Optional[dict] = None) -> dict:
        session = await self._http()
        async with session.post(
                f"{_API}/{method}", json=json or {},
                headers={"Authorization": f"Bearer {token}"}) as resp:
            payload = await resp.json(content_type=None)
        self._check_ok(method, payload)
        return payload

    @staticmethod
    def _check_ok(method: str, payload: dict) -> None:
        if payload.get("ok"):
            return
        error = payload.get("error")
        hint = ""
        if error == "not_in_channel":
            hint = (" — the bot is not a member of that channel; invite it "
                    "with /invite @<bot> (or use a DM)")
        raise RuntimeError(f"slack {method} failed: {error}{hint}")

    @staticmethod
    def _is_user_id(target: str) -> bool:
        """Slack USER ids (U…/W…) vs conversation ids (C…/G…/D…)."""
        t = str(target or "")
        return len(t) > 1 and t[0] in ("U", "W") and t[1:].isalnum() \
            and t[1:].upper() == t[1:]

    async def _resolve_channel(self, target: str) -> str:
        """``chat.postMessage`` accepts conversation ids only — a bare user id
        (a `message()` tool / sink target) silently lands in Slackbot or fails
        with channel_not_found. Open (or reuse) the app's DM conversation."""
        t = str(target)
        if not self._is_user_id(t):
            return t
        cached = self._dm_channel_cache.get(t)
        if cached:
            return cached
        channel = await self.open_dm(t)
        if channel:
            self._dm_channel_cache[t] = channel
            return channel
        return t

    async def send_message(self, channel: str, text: str,
                           thread_ts: Optional[str] = None) -> dict:
        body: dict = {"channel": await self._resolve_channel(channel),
                      "text": text}
        if thread_ts:
            body["thread_ts"] = str(thread_ts)
        return await self._call("chat.postMessage", token=self._bot_token,
                                json=body)

    async def edit_message(self, channel: str, ts: str, text: str) -> dict:
        return await self._call("chat.update", token=self._bot_token,
                                json={"channel": channel, "ts": str(ts),
                                      "text": text})

    async def open_dm(self, user_id: str) -> str:
        payload = await self._call("conversations.open", token=self._bot_token,
                                   json={"users": str(user_id)})
        return str((payload.get("channel") or {}).get("id") or "")

    async def auth_test(self) -> dict:
        return await self._call("auth.test", token=self._bot_token)

    async def connections_open(self) -> str:
        payload = await self._call("apps.connections.open",
                                   token=self._app_token)
        return str(payload.get("url") or "")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
