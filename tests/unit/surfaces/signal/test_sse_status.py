"""OS4 — the Signal SSE stream checks the HTTP status: a wrong URL (404) is
an error that is logged and backs off, not a silent 1 s reconnect forever."""
import asyncio

import surfaces.signal.client as sc
from surfaces.signal.client import SignalEventStream


class _Resp:
    def __init__(self, status, lines):
        self.status = status
        self._lines = lines

    @property
    def content(self):
        lines = self._lines

        class _It:
            def __aiter__(self):
                self._i = iter(lines)
                return self

            async def __anext__(self):
                try:
                    return next(self._i)
                except StopIteration:
                    raise StopAsyncIteration
        return _It()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fake_session(responses):
    class _Session:
        def __init__(self, *a, **kw):
            pass

        def get(self, url):
            return responses.pop(0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False
    return _Session


def test_non_200_is_logged_and_backs_off(monkeypatch, caplog):
    import aiohttp
    stream = SignalEventStream("http://127.0.0.1:8080")
    responses = [_Resp(404, [b"<html>not found</html>\n"]) for _ in range(3)]
    monkeypatch.setattr(aiohttp, "ClientSession", _fake_session(responses))
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            stream._stopped.set()

    monkeypatch.setattr(sc.asyncio, "sleep", fake_sleep)

    async def handler(env):
        raise AssertionError("no event expected")

    with caplog.at_level("WARNING"):
        asyncio.run(stream.run(handler))
    assert sleeps == [1.0, 2.0, 4.0]
    assert any("404" in r.getMessage() for r in caplog.records)


def test_backoff_resets_after_a_real_event(monkeypatch):
    import aiohttp
    stream = SignalEventStream("http://127.0.0.1:8080")
    ev = b'data: {"envelope": {"source": "+1"}}\n'
    responses = [_Resp(200, []), _Resp(200, [ev]), _Resp(200, [])]
    monkeypatch.setattr(aiohttp, "ClientSession", _fake_session(responses))
    sleeps, got = [], []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            stream._stopped.set()

    monkeypatch.setattr(sc.asyncio, "sleep", fake_sleep)

    async def handler(env):
        got.append(env)

    asyncio.run(stream.run(handler))
    assert got == [{"source": "+1"}]
    # empty 200 → no reset (1 → 2); an event resets to 1; empty again → 1 then 2
    assert sleeps == [1.0, 1.0, 2.0]
