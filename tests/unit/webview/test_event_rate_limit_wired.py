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


# ── H2c (E5-Minor): _event_emissions is bounded, so tracking one entry per
# session forever (a slow key-space leak) can't grow the dict without bound. ──

def test_event_emissions_is_bounded_dict():
    from utils.bounded_collections import BoundedDict
    import webview.server as server

    assert isinstance(server._event_emissions, BoundedDict)


def test_event_emissions_evicts_past_cap(monkeypatch):
    import webview.server as server

    server._event_emissions.clear()
    monkeypatch.setattr(server._event_emissions, "max_size", 5)

    for i in range(20):
        server.check_event_rate_limit(f"session-{i}")

    # Never grows past the cap, even after tracking 20 distinct sessions.
    assert len(server._event_emissions) <= 5
    # The oldest sessions were evicted; the most recent survive.
    assert "session-19" in server._event_emissions
    assert "session-0" not in server._event_emissions


def test_event_rate_limit_still_enforced_for_active_session_after_bounding(monkeypatch):
    import webview.server as server

    server._event_emissions.clear()
    monkeypatch.setattr(server, "RATE_LIMIT_MAX_EVENTS", 3)
    session = "active-session-under-cap"

    for _ in range(3):
        assert server.check_event_rate_limit(session) is True
    assert server.check_event_rate_limit(session) is False
