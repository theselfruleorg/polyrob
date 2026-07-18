"""E5 — check_event_rate_limit existed but was never called. Wire it into the
feed_watcher's emit path so a runaway/malicious session can't flood a Socket.IO
room with unbounded feed_update events."""
import pytest


@pytest.mark.asyncio
async def test_emit_feed_event_drops_over_limit(monkeypatch):
    import webview.server as server

    emitted = []

    async def _fake_emit(event, data=None, room=None):
        emitted.append((event, room))

    monkeypatch.setattr(server._sio, "emit", _fake_emit)
    monkeypatch.setattr(server, "RATE_LIMIT_MAX_EVENTS", 3)
    room = "rate-limited-session"

    for i in range(5):
        await server._emit_feed_event({"type": "x", "i": i}, room)

    assert len(emitted) == 3, f"expected exactly 3 emits (the cap), got {len(emitted)}"


@pytest.mark.asyncio
async def test_emit_feed_event_allows_under_limit(monkeypatch):
    import webview.server as server

    emitted = []

    async def _fake_emit(event, data=None, room=None):
        emitted.append((event, room))

    monkeypatch.setattr(server._sio, "emit", _fake_emit)
    ok = await server._emit_feed_event({"type": "x"}, "fresh-session-xyz")
    assert ok is True
    assert emitted == [("feed_update", "fresh-session-xyz")]


# ── H2c (E5-Minor): the event limiter's key space is bounded, so tracking one
# entry per session forever (a slow key-space leak) can't grow it without bound.
# F-1 (2026-07-17): the tracking dict is now inside the canonical
# core.rate_limit.SlidingWindowLimiter (max_keys LRU bound) — same invariants,
# asserted through the limiter instance. ──

def test_event_limiter_is_bounded():
    from core.rate_limit import SlidingWindowLimiter
    import webview.server as server

    assert isinstance(server._event_limiter, SlidingWindowLimiter)
    assert server._event_limiter._max_keys == server.EVENT_EMISSIONS_MAX_SESSIONS


def test_event_limiter_evicts_past_cap(monkeypatch):
    import webview.server as server

    server._event_limiter._calls.clear()
    monkeypatch.setattr(server._event_limiter, "_max_keys", 5)

    for i in range(20):
        server.check_event_rate_limit(f"session-{i}")

    # Never grows past the cap, even after tracking 20 distinct sessions.
    assert len(server._event_limiter._calls) <= 5
    # The oldest sessions were evicted; the most recent survive.
    assert "session-19" in server._event_limiter.keys()
    assert "session-0" not in server._event_limiter.keys()


def test_event_rate_limit_still_enforced_for_active_session_after_bounding(monkeypatch):
    import webview.server as server

    server._event_limiter._calls.clear()
    monkeypatch.setattr(server, "RATE_LIMIT_MAX_EVENTS", 3)
    session = "active-session-under-cap"

    for _ in range(3):
        assert server.check_event_rate_limit(session) is True
    assert server.check_event_rate_limit(session) is False
