"""DC1 — a fatal gateway close code stops the loop loudly; the backoff resets
only after READY; the probe checks the MESSAGE_CONTENT intent."""
import asyncio
import json
import types

import pytest

import surfaces.discord.gateway as gw
from surfaces.discord.gateway import DiscordGatewayClient, DiscordGatewayFatal


class _ClosedWS:
    """A socket the server closed with ``close_code`` (no frames, or some)."""

    def __init__(self, close_code, frames=()):
        import aiohttp
        self.close_code = close_code
        self._frames = [types.SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                                              data=json.dumps(f)) for f in frames]
        self.sent = []

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i < len(self._frames):
            self._i += 1
            return self._frames[self._i - 1]
        raise StopAsyncIteration

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        pass


async def _noop(d):
    return None


@pytest.mark.parametrize("code", [4004, 4010, 4011, 4012, 4013, 4014])
def test_fatal_close_code_raises(code):
    client = DiscordGatewayClient("tok", lambda: "url")
    with pytest.raises(DiscordGatewayFatal) as ei:
        asyncio.run(client._consume(_ClosedWS(code), _noop))
    assert str(code) in str(ei.value)


def test_non_fatal_close_code_returns():
    client = DiscordGatewayClient("tok", lambda: "url")
    asyncio.run(client._consume(_ClosedWS(4000), _noop))   # no raise


def test_run_stops_on_fatal_close(monkeypatch, caplog):
    client = DiscordGatewayClient("tok", lambda: "url")
    calls = []

    async def once(handler):
        calls.append(1)
        raise DiscordGatewayFatal(4014, "Disallowed intent(s)")

    monkeypatch.setattr(client, "_connect_once", once)
    with caplog.at_level("ERROR"):
        with pytest.raises(DiscordGatewayFatal):
            asyncio.run(asyncio.wait_for(client.run(_noop), timeout=2))
    assert calls == [1]                       # no re-IDENTIFY loop
    assert any("4014" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")


def test_backoff_resets_only_after_ready(monkeypatch):
    client = DiscordGatewayClient("tok", lambda: "url")
    sleeps = []
    script = iter([False, False, True, False, None])

    async def once(handler):
        nxt = next(script)
        if nxt is None:
            client._stopped.set()
            return False
        return nxt

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(client, "_connect_once", once)
    monkeypatch.setattr(gw.asyncio, "sleep", fake_sleep)
    asyncio.run(client.run(_noop))
    # no READY: 1 → 2 → (READY resets) 1 → 2
    assert sleeps == [1.0, 2.0, 1.0, 2.0]


def test_probe_fails_without_message_content_intent(monkeypatch):
    import surfaces.discord.probe as probe_mod

    async def fake_http_json(method, url, **kw):
        if url.endswith("/users/@me"):
            return 200, {"username": "rob"}, ""
        return 200, {"flags": 0}, ""

    monkeypatch.setattr(probe_mod, "http_json", fake_http_json)
    res = asyncio.run(probe_mod.probe({"DISCORD_BOT_TOKEN": "t"}))
    assert res.state == "failed"
    assert "MESSAGE_CONTENT" in res.detail


@pytest.mark.parametrize("flags", [1 << 18, 1 << 19])
def test_probe_ok_with_message_content_intent(monkeypatch, flags):
    import surfaces.discord.probe as probe_mod

    async def fake_http_json(method, url, **kw):
        if url.endswith("/users/@me"):
            return 200, {"username": "rob"}, ""
        return 200, {"flags": flags}, ""

    monkeypatch.setattr(probe_mod, "http_json", fake_http_json)
    res = asyncio.run(probe_mod.probe({"DISCORD_BOT_TOKEN": "t"}))
    assert res.state == "ok"
