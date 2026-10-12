"""D4 (prod 2026-10-09/10): a Bot API 502 streak met a flat 1 s retry — ~30
getUpdates calls in 45 s. A failing poll now backs off exponentially (capped,
jittered) and a success resets it; flood control keeps its exact wait."""
import asyncio

from surfaces.telegram.harness import TelegramHarness
from surfaces.telegram.poll_backoff import CAP_SEC, poll_backoff_sec


def test_backoff_doubles_and_caps():
    full = [poll_backoff_sec(n, rand=lambda: 1.0) for n in range(1, 9)]
    assert full == [1, 2, 4, 8, 16, 30, 30, 30]
    half = [poll_backoff_sec(n, rand=lambda: 0.0) for n in range(1, 4)]
    assert half == [0.5, 1, 2]
    assert poll_backoff_sec(10_000) <= CAP_SEC


class _BadGateway(Exception):
    pass


_BadGateway.__name__ = "TelegramServerError"


def _harness(script):
    """get_updates follows *script*: an exception instance raises, a list returns."""
    h = object.__new__(TelegramHarness)
    h._running = True
    h.poll_timeout = 0
    it = iter(script)

    class _Bot:
        async def get_updates(self, **kw):
            try:
                step = next(it)
            except StopIteration:
                h._running = False
                return []
            if isinstance(step, BaseException):
                raise step
            return step

    h.bot = _Bot()
    return h


def _run(h, monkeypatch):
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    monkeypatch.setattr("surfaces.telegram.poll_backoff.random.random", lambda: 1.0)
    asyncio.run(h.run_polling())
    return slept


def test_a_502_streak_backs_off_instead_of_hammering(monkeypatch):
    h = _harness([_BadGateway("Bad Gateway")] * 6)
    assert _run(h, monkeypatch) == [1, 2, 4, 8, 16, 30]


def test_a_success_resets_the_backoff(monkeypatch):
    err = _BadGateway("Bad Gateway")
    h = _harness([err, err, err, [], err])
    assert _run(h, monkeypatch) == [1, 2, 4, 1]


def test_flood_control_keeps_its_retry_after(monkeypatch):
    flood = type("TelegramRetryAfter", (Exception,), {})("Flood control")
    flood.retry_after = 7
    h = _harness([flood])
    assert _run(h, monkeypatch) == [7]
