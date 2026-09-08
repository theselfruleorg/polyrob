"""Thin Discord REST client (API v10) over aiohttp. Bot-token auth.

Only the endpoints the surface needs: send/edit messages, typing, DM-channel
creation, and the gateway URL. Fail-open callers handle exceptions.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_API = "https://discord.com/api/v10"
_DISCORD_MAX_CONTENT = 2000


class DiscordClient:
    def __init__(self, token: Optional[str] = None) -> None:
        self._token = token or os.getenv("DISCORD_BOT_TOKEN", "")
        self._session = None  # lazy aiohttp.ClientSession

    async def _http(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bot {self._token}",
                         "User-Agent": "polyrob (https://github.com/theselfruleorg/polyrob, 0.5)"})
        return self._session

    async def _request(self, method: str, path: str,
                       json: Optional[dict] = None) -> Any:
        session = await self._http()
        async with session.request(method, f"{_API}{path}", json=json) as resp:
            if resp.status == 204:
                return None
            payload = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(
                    f"discord {method} {path} -> {resp.status}: {payload}")
            return payload

    async def send_message(self, channel_id: str, text: str,
                           reply_to: Optional[str] = None) -> dict:
        body: dict = {"content": text}
        if reply_to:
            body["message_reference"] = {"message_id": str(reply_to),
                                         "fail_if_not_exists": False}
        return await self._request("POST", f"/channels/{channel_id}/messages",
                                   json=body)

    async def send_file(self, channel_id: str, path: str,
                        filename: Optional[str] = None,
                        content: Optional[str] = None) -> dict:
        """Upload one file as a message attachment (multipart, 030 D6)."""
        import json as _json
        import aiohttp
        session = await self._http()
        form = aiohttp.FormData()
        payload: dict = {"attachments": [{"id": 0,
                                          "filename": filename or "file"}]}
        if content:
            payload["content"] = str(content)[:_DISCORD_MAX_CONTENT]
        form.add_field("payload_json", _json.dumps(payload),
                       content_type="application/json")
        with open(path, "rb") as fh:
            form.add_field("files[0]", fh.read(), filename=filename or "file",
                           content_type="application/octet-stream")
        async with session.post(f"{_API}/channels/{channel_id}/messages",
                                data=form) as resp:
            payload = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(
                    f"discord file upload -> {resp.status}: {payload}")
            return payload

    async def edit_message(self, channel_id: str, message_id: str,
                           text: str) -> dict:
        return await self._request(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}",
            json={"content": text})

    async def trigger_typing(self, channel_id: str) -> None:
        await self._request("POST", f"/channels/{channel_id}/typing")

    async def create_dm(self, user_id: str) -> str:
        """Open (or fetch) the DM channel with *user_id*; returns channel id."""
        payload = await self._request("POST", "/users/@me/channels",
                                      json={"recipient_id": str(user_id)})
        return str(payload["id"])

    async def get_gateway_url(self) -> str:
        payload = await self._request("GET", "/gateway/bot")
        return str(payload["url"])

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
