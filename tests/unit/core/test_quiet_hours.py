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
    # 2026-09-15 (C10): the rail resolves preferences on the IDENTITY axis (the
    # DATA HOME), not from `container.config.data_dir` — on a server the latter
    # is `<data_home>/data`, a shadow no preference writer writes to. Pin the
    # data home so the pref this fixture writes is the one the rail reads.
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
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


# ---------------------------------------------------------------------------
# 2026-10-03 audit: OB4 / OB9 / OB10 / OB11
# ---------------------------------------------------------------------------

class _MediaSink:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, media=None, actions=None):
        self.sent.append({"chat_id": chat_id, "text": text, "media": media})
        return True


def test_release_replays_the_full_delivery(rail, monkeypatch, tmp_path):
    """OB4: a hold kept text[:4000] only and the release went to the owner's
    default surface — a room's cron report landed in the owner DM, without its
    attachments."""
    from core.surfaces.user_delivery import deliver_user_message, release_quiet_held
    _, log, container, qh = rail
    sink = _MediaSink()
    container._sink = sink
    monkeypatch.setattr("core.surfaces.spill.maybe_spill", lambda *a, **k: None)
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF")
    att = [{"kind": "document", "path": str(doc), "caption": None}]
    long_body = "R" * 6000
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 23)
    out = _run(deliver_user_message(container, "u1", long_body, source="cron",
                                    recipient_override="-100777",
                                    attachments=att, event_log=log))
    assert out == "quiet_held"
    held = [e for e in log.events if e["attrs"].get("outcome") == "quiet_held"][0]
    assert len(held["attrs"]["held_text"]) == 6000
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 9)
    assert _run(release_quiet_held(container, event_log=log)) == 1
    assert len(sink.sent) == 1
    assert sink.sent[0]["chat_id"] == "-100777"          # the room, not the owner
    assert sink.sent[0]["text"] == long_body
    assert sink.sent[0]["media"] == att


def test_a_failed_hold_write_sends_instead_of_losing(rail, monkeypatch):
    """OB9: a hold that could not be written still returned quiet_held."""
    from core.surfaces.user_delivery import deliver_user_message
    sink, log, container, qh = rail
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 23)
    real = log.record

    def _record(kind, **kw):
        if (kw.get("attrs") or {}).get("outcome") == "quiet_held":
            raise RuntimeError("disk full")
        return real(kind, **kw)

    log.record = _record
    out = _run(deliver_user_message(container, "u1", "must not vanish",
                                    source="cron", event_log=log))
    assert out == "sent"
    assert [t for _, t in sink.sent] == ["must not vanish"]


def test_a_no_sink_release_ends_the_hold(rail, monkeypatch):
    """OB10: deduped/no_sink were not terminal, so the hold re-sent every tick
    for 48 h."""
    from core.surfaces.user_delivery import deliver_user_message, release_quiet_held
    sink, log, container, qh = rail
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 23)
    assert _run(deliver_user_message(container, "u1", "held body", source="cron",
                                     event_log=log)) == "quiet_held"
    container._sink = None                     # no live sink at release time
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 9)
    _run(release_quiet_held(container, event_log=log))
    n_rows = len(log.events)
    _run(release_quiet_held(container, event_log=log))
    _run(release_quiet_held(container, event_log=log))
    assert len(log.events) == n_rows           # no new attempt per tick


def test_release_reads_past_the_newest_thousand_rows(rail, monkeypatch):
    """OB10 (D48 class): an older hold behind 1000 newer rows was never seen."""
    from core.surfaces.user_delivery import (
        DELIVERY_EVENT_KIND, deliver_user_message, release_quiet_held)
    sink, log, container, qh = rail
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 23)
    assert _run(deliver_user_message(container, "u1", "old held", source="cron",
                                     event_log=log)) == "quiet_held"
    t0 = time.time()
    for i in range(1100):
        log.record(DELIVERY_EVENT_KIND, user_id="u9", source="agent", ts=t0 + 1 + i,
                   attrs={"outcome": "deduped", "content_hash": f"x{i}"})
    def _count_where(**kw):
        if set(kw) - {"kind", "since_ts"}:
            raise NotImplementedError   # the rail's filtered counts: in memory
        return len([e for e in log.events
                    if e["kind"] == kw.get("kind") and e["ts"] >= (kw.get("since_ts") or 0)])

    log.count_where = _count_where
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 9)
    assert _run(release_quiet_held(container, event_log=log, now=t0 + 2000)) == 1
    assert [t for _, t in sink.sent] == ["old held"]


def test_a_room_delivery_does_not_dedup_the_owner_copy(rail, monkeypatch):
    """OB11: content dedup ignored the recipient, so a room delivery blocked the
    same text to the owner for 24 h."""
    from core.surfaces.user_delivery import deliver_user_message
    sink, log, container, qh = rail
    monkeypatch.setattr(qh, "_now_hour_local", lambda: 12)
    assert _run(deliver_user_message(container, "u1", "daily report", source="cron",
                                     recipient_override="-100777",
                                     event_log=log)) == "sent"
    assert _run(deliver_user_message(container, "u1", "daily report", source="cron",
                                     event_log=log)) == "sent"
    assert _run(deliver_user_message(container, "u1", "daily report", source="cron",
                                     event_log=log)) == "deduped"
    assert [c for c, _ in sink.sent] == ["-100777", "42"]


def test_quiet_window_reads_the_owner_timezone(tmp_path, monkeypatch):
    """OB8: the window used the server clock (UTC on prod). With the owner's
    IANA zone set, the window is read in that zone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from core.surfaces import quiet_hours
    write_preference(tmp_path, "u1", "digest.quiet_hours", "22-06")
    monkeypatch.setattr(quiet_hours, "_now_hour_local", lambda: 12)   # server noon
    monkeypatch.setattr(quiet_hours, "effective_timezone",
                        lambda uid, home: "Pacific/Kiritimati")
    hour = datetime.now(ZoneInfo("Pacific/Kiritimati")).hour
    expect = hour >= 22 or hour < 6
    assert quiet_hours.quiet_window_active("u1", tmp_path) is expect
    monkeypatch.setattr(quiet_hours, "_now_hour_in", lambda tz: 23)
    assert quiet_hours.quiet_window_active("u1", tmp_path) is True


def test_unknown_or_unset_timezone_keeps_the_server_clock(tmp_path, monkeypatch):
    from core.surfaces import quiet_hours
    write_preference(tmp_path, "u1", "digest.quiet_hours", "22-06")
    monkeypatch.setattr(quiet_hours, "_now_hour_local", lambda: 23)
    assert quiet_hours.quiet_window_active("u1", tmp_path) is True        # unset
    monkeypatch.setattr(quiet_hours, "effective_timezone", lambda uid, home: "Mars/Olympus")
    assert quiet_hours.quiet_window_active("u1", tmp_path) is True        # unknown
    assert quiet_hours._now_hour_in("Mars/Olympus") is None
    assert quiet_hours._now_hour_in("UTC") in range(24)


def test_digest_timezone_is_a_registered_owner_pref(tmp_path):
    """OB8 finish: `/prefs` can set and show digest.timezone — the pref is in
    the schema, owner-safe, and validated with zoneinfo."""
    from core.prefs import PREF_SCHEMA, SENSITIVITY_SAFE, validate_pref
    from core.surfaces import quiet_hours
    spec = PREF_SCHEMA["digest.timezone"]
    assert spec.type == "str" and spec.sensitivity == SENSITIVITY_SAFE
    assert validate_pref("digest.timezone", " Europe/Berlin ") == (True, "Europe/Berlin", "")
    ok, _, err = validate_pref("digest.timezone", "Mars/Olympus")
    assert not ok and "Mars/Olympus" in err and "IANA" in err
    ok, _, err = validate_pref("digest.timezone", "../../etc/passwd")
    assert not ok
    assert validate_pref("digest.timezone", "") == (True, "", "")     # server clock
    ok, err = write_preference(tmp_path, "u1", "digest.timezone", "Asia/Bangkok")
    assert ok, err
    assert quiet_hours.effective_timezone("u1", tmp_path) == "Asia/Bangkok"
    ok, err = write_preference(tmp_path, "u1", "digest.timezone", "Nowhere/Land")
    assert not ok
    assert quiet_hours.effective_timezone("u1", tmp_path) == "Asia/Bangkok"
