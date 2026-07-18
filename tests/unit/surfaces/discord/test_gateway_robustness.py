"""Finalization hardening — gateway half-open detection + op 7/9/1 handling."""
import asyncio
import json
import types

import surfaces.discord.gateway as gw
from surfaces.discord.gateway import DiscordGatewayClient


class _FakeWS:
    """Scripted TEXT frames; optionally holds the read open until close()."""

    def __init__(self, frames, hold_open=False):
        import aiohttp
        self._frames = [types.SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                                              data=json.dumps(f))
                        for f in frames]
        self._hold_open = hold_open
        self.sent = []
        self.closed = False
        self._closed_event = asyncio.Event()

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i < len(self._frames):
            frame = self._frames[self._i]
            self._i += 1
            return frame
        if self._hold_open and not self.closed:
            await self._closed_event.wait()
        raise StopAsyncIteration

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True
        self._closed_event.set()


def _hello(interval_ms=10):
    return {"op": 10, "d": {"heartbeat_interval": interval_ms}}


async def _noop_handler(d):
    return None


def test_hello_sends_identify():
    ws = _FakeWS([_hello(interval_ms=60000)])
    client = DiscordGatewayClient("tok", lambda: "url")
    asyncio.run(client._consume(ws, _noop_handler))
    identifies = [p for p in ws.sent if p.get("op") == 2]
    assert len(identifies) == 1
    assert identifies[0]["d"]["token"] == "tok"


def test_missed_heartbeat_ack_forces_reconnect():
    """A half-open socket never sends HEARTBEAT_ACK; the second beat must
    force-close the WS so run() reconnects instead of hanging forever."""

    async def scenario():
        ws = _FakeWS([_hello(interval_ms=10)], hold_open=True)
        client = DiscordGatewayClient("tok", lambda: "url")
        await asyncio.wait_for(client._consume(ws, _noop_handler), timeout=2)
        assert ws.closed is True
        beats = [p for p in ws.sent if p.get("op") == 1]
        assert len(beats) == 1  # first beat sent; second detected the miss

    asyncio.run(scenario())


def test_acked_heartbeats_do_not_force_close():
    """When ACK frames arrive between beats, the watchdog must never close."""
    frames = [_hello(interval_ms=10)] + [{"op": 11}] * 3
    ws = _FakeWS(frames)  # iterator ends after the ACKs → clean exit
    client = DiscordGatewayClient("tok", lambda: "url")
    asyncio.run(asyncio.wait_for(client._consume(ws, _noop_handler), timeout=2))
    assert ws.closed is False


def test_op_reconnect_rotates():
    ws = _FakeWS([_hello(interval_ms=60000), {"op": 7}], hold_open=True)
    client = DiscordGatewayClient("tok", lambda: "url")
    asyncio.run(asyncio.wait_for(client._consume(ws, _noop_handler), timeout=2))
    # returned promptly despite hold_open (op 7 exits the consume loop)


def test_op_invalid_session_waits_then_rotates(monkeypatch):
    monkeypatch.setattr(gw, "_INVALID_SESSION_DELAY_SEC", 0.0)
    ws = _FakeWS([_hello(interval_ms=60000), {"op": 9}], hold_open=True)
    client = DiscordGatewayClient("tok", lambda: "url")
    asyncio.run(asyncio.wait_for(client._consume(ws, _noop_handler), timeout=2))


def test_server_heartbeat_request_answered():
    ws = _FakeWS([_hello(interval_ms=60000), {"op": 1, "s": 5}])
    client = DiscordGatewayClient("tok", lambda: "url")
    asyncio.run(client._consume(ws, _noop_handler))
    beats = [p for p in ws.sent if p.get("op") == 1]
    assert beats and beats[-1]["d"] == 5
