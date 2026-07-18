"""P0.3 (proposal 018): digest.quiet_hours actually enforces.

Owner decision (2026-07-18): DEFER to window-end — a proactive send inside the
quiet window is durably held (event-log row, outcome ``quiet_held``) and
released by ``release_quiet_held`` once the window ends; interactive replies
never route through this rail so they are never gated. Fail-open: no event log
=> no durable hold is possible => send (the rail's existing posture).
"""
import time

import pytest

from core.prefs import write_preference


# ---------------------------------------------------------------------------
# window parsing / membership
# ---------------------------------------------------------------------------

def test_parse_quiet_window():
    from core.surfaces.quiet_hours import parse_quiet_window
    assert parse_quiet_window("23-08") == (23, 8)
    assert parse_quiet_window("13-15") == (13, 15)
    assert parse_quiet_window("8-8") is None      # zero-length window
    assert parse_quiet_window("junk") is None
    assert parse_quiet_window(None) is None
    assert parse_quiet_window("25-08") is None    # invalid hour


def test_in_quiet_window_wrapping_and_plain():
    from core.surfaces.quiet_hours import in_quiet_window
    for hour, expect in ((23, True), (0, True), (7, True), (8, False), (12, False)):
        assert in_quiet_window(hour, (23, 8)) is expect, hour
    for hour, expect in ((13, True), (14, True), (15, False), (12, False)):
        assert in_quiet_window(hour, (13, 15)) is expect, hour


def test_quiet_window_active_reads_tenant_pref(tmp_path, monkeypatch):
    from core.surfaces import quiet_hours
    write_preference(tmp_path, "u1", "digest.quiet_hours", "22-06")
    monkeypatch.setattr(quiet_hours, "_now_hour_local", lambda: 23)
    assert quiet_hours.quiet_window_active("u1", tmp_path) is True
    monkeypatch.setattr(quiet_hours, "_now_hour_local", lambda: 12)
    assert quiet_hours.quiet_window_active("u1", tmp_path) is False
    # No pref set => never active.
    assert quiet_hours.quiet_window_active("u2", tmp_path) is False


# ---------------------------------------------------------------------------
# rail integration: hold + release
# ---------------------------------------------------------------------------

class _FakeEventLog:
    def __init__(self):
        self.events = []

    def record(self, kind, *, user_id="", session_id="", source="", ts=None,
               attrs=None, **kw):
        merged = dict(kw)
        merged.update(attrs or {})
        self.events.append({"kind": kind, "user_id": user_id,
                            "session_id": session_id, "source": source,
                            "ts": ts if ts is not None else time.time(),
                            "attrs": merged})

    def query(self, *, since_ts=None, kind=None, user_id=None, limit=500):
        out = [e for e in self.events
               if (since_ts is None or e["ts"] >= since_ts)
               and (kind is None or e["kind"] == kind)
               and (user_id is None or e["user_id"] == user_id)]
        return sorted(out, key=lambda e: e["ts"], reverse=True)[:limit]


class _FakeSink:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _FakeContainer:
    def __init__(self, sink, data_dir):
        self._sink = sink

        class _Cfg:
            pass

        self.config = _Cfg()
        self.config.data_dir = str(data_dir)

    def get_service(self, name):
        if name == "telegram_sink":
            return self._sink
        return None


@pytest.fixture()
def rail(tmp_path, monkeypatch):
    from core.surfaces import quiet_hours
    sink = _FakeSink()
    log = _FakeEventLog()
    container = _FakeContainer(sink, tmp_path)
    # Recipient: owner-principal fallback path — pin it directly instead.
    monkeypatch.setattr("core.surfaces.user_delivery._resolve_recipient",
                        lambda c, uid: "42")
    write_preference(tmp_path, "u1", "digest.quiet_hours", "22-06")
    return sink, log, container, quiet_hours


def _run(coro):
    import asyncio
    # asyncio.run (not get_event_loop): after any pytest-asyncio test the main
    # thread has no current loop, so get_event_loop() raises RuntimeError —
    # this file then fails whenever it runs after an async suite.
    return asyncio.run(coro)


def test_hold_inside_window_then_release_after(rail, monkeypatch):
    from core.surfaces.user_delivery import deliver_user_message, release_quiet_held
    sink, log, container, qh = rail

    monkeypatch.setattr(qh, "_now_hour_local", lambda: 23)
    out = _run(deliver_user_message(container, "u1", "good night report",
                                    source="cron", event_log=log))
    assert out == "quiet_held"
    assert sink.sent == []
    held = [e for e in log.events
            if (e["attrs"] or {}).get("outcome") == "quiet_held"]
    assert held and "good night report" in (held[0]["attrs"].get("held_text") or "")

    # Still inside the window: release is a no-op.
    assert _run(release_quiet_held(container, event_log=log)) == 0
    assert sink.sent == []

    # Window over: the held message is delivered exactly once.
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 9)
    assert _run(release_quiet_held(container, event_log=log)) == 1
    assert [t for _, t in sink.sent] == ["good night report"]
    # Idempotent: a second sweep must not re-send (consumed outcome recorded).
    assert _run(release_quiet_held(container, event_log=log)) == 0
    assert len(sink.sent) == 1


def test_outside_window_sends_normally(rail, monkeypatch):
    from core.surfaces.user_delivery import deliver_user_message
    sink, log, container, qh = rail
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 12)
    out = _run(deliver_user_message(container, "u1", "midday note",
                                    source="agent", event_log=log))
    assert out == "sent"
    assert [t for _, t in sink.sent] == ["midday note"]


def test_no_event_log_fails_open_to_send(rail, monkeypatch):
    # No durable store => a hold would silently lose the message; send instead.
    from core.surfaces.user_delivery import deliver_user_message
    sink, log, container, qh = rail
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 23)
    out = _run(deliver_user_message(container, "u1", "no log around",
                                    source="agent", event_log=None))
    assert out == "sent"
    assert [t for _, t in sink.sent] == ["no log around"]
