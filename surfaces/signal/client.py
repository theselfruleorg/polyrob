"""Thin signal-cli HTTP daemon client: JSON-RPC send + SSE event stream.

Daemon: ``signal-cli -a +<E164> daemon --http=127.0.0.1:8080`` exposes
JSON-RPC at ``/api/v1/rpc`` and an SSE event feed at ``/api/v1/events``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def extract_envelope(payload: dict) -> dict:
    """Unwrap an SSE frame to the signal-cli envelope dict.

    Native SSE frames are ``{"envelope": {...}}``; some builds emit the stdio
    JSON-RPC notification shape ``{"method": "receive", "params":
    {"envelope": {...}}}``. Without unwrapping both, ``parse_envelope`` sees
    no ``dataMessage`` and silently drops the message."""
    if not isinstance(payload, dict):
        return {}
    return (payload.get("envelope")
            or (payload.get("params") or {}).get("envelope")
            or payload)


class SignalClient:
    def __init__(self, daemon_url: Optional[str] = None,
                 account: Optional[str] = None) -> None:
        self.daemon_url = (daemon_url or os.getenv("SIGNAL_DAEMON_URL")
                           or "http://127.0.0.1:8080").rstrip("/")
        self.account = account or os.getenv("SIGNAL_ACCOUNT", "")
        self._session = None
        self._rpc_id = 0

    async def _http(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, recipient: str, text: str) -> Any:
        """JSON-RPC ``send`` to one recipient (E164) or group id."""
        self._rpc_id += 1
        params: dict = {"message": text}
        if self.account:
            # Only pass account when configured — an empty string can be
            # rejected or mis-routed by a multi-account daemon.
            params["account"] = self.account
        if recipient.startswith("group."):
            params["groupId"] = recipient[len("group."):]
        else:
            params["recipient"] = [recipient]
        body = {"jsonrpc": "2.0", "id": self._rpc_id, "method": "send",
                "params": params}
        session = await self._http()
        async with session.post(f"{self.daemon_url}/api/v1/rpc",
                                json=body) as resp:
            payload = await resp.json(content_type=None)
        if payload.get("error"):
            raise RuntimeError(f"signal send failed: {payload['error']}")
        return payload.get("result")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class SignalEventStream:
    """Consume the daemon's SSE feed; ``handler`` gets each ``envelope`` dict."""

    def __init__(self, daemon_url: str) -> None:
        self._url = daemon_url.rstrip("/") + "/api/v1/events"
        self._stopped = asyncio.Event()

    async def stop(self) -> None:
        self._stopped.set()

    async def run(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        import aiohttp
        backoff = 1.0
        while not self._stopped.is_set():
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(self._url) as resp:
                        backoff = 1.0
                        async for raw in resp.content:
                            if self._stopped.is_set():
                                return
                            line = raw.decode("utf-8", "replace").strip()
                            if not line.startswith("data:"):
                                continue
                            try:
                                payload = json.loads(line[len("data:"):].strip())
                            except json.JSONDecodeError:
                                continue
                            envelope = extract_envelope(payload)
                            try:
                                await handler(envelope)
                            except Exception:
                                logger.warning("signal inbound handler failed",
                                               exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("signal event stream error: %s — reconnecting "
                               "in %.0fs", e, backoff)
            if self._stopped.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
