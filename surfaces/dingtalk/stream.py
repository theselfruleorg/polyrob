"""DingTalk Stream Mode, hand-rolled on aiohttp (no SDK).

1. ``POST https://api.dingtalk.com/v1.0/gateway/connections/open`` with the app's
   Client ID / Secret and the subscriptions → ``{endpoint, ticket}``.
2. ``ws_connect(endpoint?ticket=…)``.
3. JSON frames: ``{specVersion, type: SYSTEM|EVENT|CALLBACK, headers: {topic,
   messageId, contentType, …}, data: "<json string>"}``. EVERY frame is
   acknowledged with its ``messageId``. ``SYSTEM`` ``ping`` echoes its data;
   ``SYSTEM`` ``disconnect`` ends this connection and the loop reconnects (a
   fresh ticket each time). A ``CALLBACK`` on the bot-message topic is acked
   FIRST and then handed to the handler as a task — never awaited, so a long
   agent turn never stalls the socket (the platform redelivers an unacked
   frame; dedup in the harness absorbs a redelivery).

The frame helpers are pure so the tests run offline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

from surfaces.dingtalk.client import API_BASE
from surfaces.dingtalk.events import TOPIC

logger = logging.getLogger(__name__)

OPEN_PATH = "/v1.0/gateway/connections/open"

#: Reconnect backoff (seconds): doubles from the first value to the cap.
_BACKOFF = (1.0, 60.0)
#: OS10: a session must stay up this long before the reconnect delay resets.
#: A server that accepts and closes at once otherwise drew a 1 s reconnect loop.
HEALTHY_SESSION_S = 60.0


def open_request(client_id: str, client_secret: str) -> dict:
    return {"clientId": client_id, "clientSecret": client_secret,
            "subscriptions": [{"type": "CALLBACK", "topic": TOPIC}],
            "ua": "polyrob"}


def connect_url(endpoint: str, ticket: str) -> str:
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}ticket={quote(ticket, safe='')}"


def ack(frame: dict, data: Optional[str] = None) -> dict:
    """The acknowledgement for one frame (the same shape for every type)."""
    headers = frame.get("headers") or {}
    return {"code": 200,
            "headers": {"contentType": "application/json",
                        "messageId": headers.get("messageId")},
            "message": "OK",
            "data": data if data is not None else json.dumps({"response": None})}


def handle_frame(frame: dict) -> "tuple[Optional[dict], Optional[dict], bool]":
    """``(ack_to_send, callback_data_to_route, disconnect)`` for one frame."""
    if not isinstance(frame, dict):
        return None, None, False
    ftype = frame.get("type")
    topic = (frame.get("headers") or {}).get("topic")
    if ftype == "SYSTEM":
        if topic == "ping":
            return ack(frame, data=frame.get("data")), None, False
        if topic == "disconnect":
            return None, None, True
        return ack(frame), None, False
    if ftype == "EVENT":
        return ack(frame, data=json.dumps({"status": "SUCCESS", "message": "success"})), \
            None, False
    if ftype == "CALLBACK":
        payload = None
        if topic == TOPIC:
            try:
                payload = json.loads(frame.get("data") or "")
            except (TypeError, ValueError):
                payload = None
            if not isinstance(payload, dict):
                payload = None
        return ack(frame), payload, False
    return None, None, False


class DingTalkStream:
    def __init__(self, client_id: str, client_secret: str, *,
                 http=None) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http          # an injectable ``(url, json) -> (status, payload)``
        self._stopping = asyncio.Event()
        self._ws = None
        self._tasks: set = set()

    async def _open(self) -> str:
        body = open_request(self._client_id, self._client_secret)
        if self._http is not None:
            status, payload = await self._http(API_BASE + OPEN_PATH, body)
        else:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s, \
                    s.post(API_BASE + OPEN_PATH, json=body) as resp:
                status = resp.status
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = None
        payload = payload if isinstance(payload, dict) else {}
        endpoint, ticket = payload.get("endpoint"), payload.get("ticket")
        if not (endpoint and ticket):
            code = payload.get("code")
            raise RuntimeError("dingtalk stream open failed: "
                               + (f"code {code}" if code else f"HTTP {status}"))
        return connect_url(str(endpoint), str(ticket))

    async def _dispatch(self, raw: str, send, handler) -> bool:
        """One text frame: ack, route; True when the server asked to disconnect."""
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            logger.debug("dingtalk: non-JSON frame dropped")
            return False
        reply, data, disconnect = handle_frame(frame)
        if reply is not None:
            await send(json.dumps(reply))
        if data is not None:
            task = asyncio.get_running_loop().create_task(handler(data))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return disconnect

    async def _session(self, handler) -> None:
        import aiohttp
        url = await self._open()
        async with aiohttp.ClientSession() as s, s.ws_connect(url, heartbeat=30) as ws:
            self._ws = ws
            logger.info("dingtalk stream connected")
            async for msg in ws:
                if self._stopping.is_set():
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if await self._dispatch(msg.data, ws.send_str, handler):
                        logger.info("dingtalk stream: server asked to disconnect — reconnecting")
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        self._ws = None

    async def run(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        delay = _BACKOFF[0]
        first = True
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                await self._session(handler)
                first = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if first:
                    # A refused credential on the first open fails loudly.
                    raise RuntimeError(f"dingtalk stream failed: {type(e).__name__}"
                                       + (f" ({e})" if str(e).startswith("dingtalk") else ""))
                if time.monotonic() - started >= HEALTHY_SESSION_S:
                    delay = _BACKOFF[0]     # it worked for a while: start over
                logger.warning("dingtalk stream dropped (%s) — retry in %.0fs",
                               type(e).__name__, delay)
            else:
                if time.monotonic() - started >= HEALTHY_SESSION_S:
                    delay = _BACKOFF[0]
            if self._stopping.is_set():
                break
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, _BACKOFF[1])

    async def stop(self) -> None:
        self._stopping.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
