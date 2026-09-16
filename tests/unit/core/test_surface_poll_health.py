"""C11 (2026-09-15 prod review): inbound transport faults are reported.

Over 7 days prod logged 76 `telegram get_updates failed` errors — 34 request
timeouts, 28 connection resets, 14 Bad Gateway. The poller recovers, and that
is the point: nothing ever surfaced the instability, so "the agent went quiet
for a while" had no reading anywhere. Recovery is not the same as health.

Not an outage alarm — a WARN with a count, above a threshold that ordinary
long-poll turbulence does not reach.
"""
import json
import sqlite3
import time

import pytest


def _tele(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE telemetry_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
        user_id TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '', attrs TEXT NOT NULL DEFAULT '{}')""")
    now = time.time()
    for r in rows:
        conn.execute("INSERT INTO telemetry_events (ts, kind, user_id, source, attrs)"
                     " VALUES (?,?,?,?,?)",
                     (now - r.get("age", 60), r["kind"], "", r.get("source", "telegram"),
                      json.dumps(r.get("attrs", {}))))
    conn.commit()
    conn.close()


def _items(tmp_path, rows):
    from core.status_snapshot import _read_telemetry, _surface_poll_health
    _tele(tmp_path / "telemetry_events.db", rows)
    tele = _read_telemetry("", str(tmp_path), time.time() - 86400)
    return {h.key: h for h in _surface_poll_health(tele)}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)


def test_sustained_poll_failures_are_reported(tmp_path):
    from core.event_kinds import SURFACE_POLL_ERROR
    rows = [{"kind": SURFACE_POLL_ERROR, "attrs": {"error": "Request timeout error"}}
            for _ in range(12)]
    items = _items(tmp_path, rows)
    assert "surface_poll_errors" in items
    assert "12" in items["surface_poll_errors"].text
    assert "telegram" in items["surface_poll_errors"].text


def test_ordinary_turbulence_is_not_an_alarm(tmp_path):
    from core.event_kinds import SURFACE_POLL_ERROR
    items = _items(tmp_path, [{"kind": SURFACE_POLL_ERROR} for _ in range(2)])
    assert items == {}


def test_a_quiet_transport_reports_nothing(tmp_path):
    assert _items(tmp_path, [{"kind": "cron_run"}]) == {}


def test_the_kind_is_in_the_catalog():
    from core.event_kinds import KNOWN_KINDS, SURFACE_POLL_ERROR
    assert SURFACE_POLL_ERROR in KNOWN_KINDS


# --- the producer -----------------------------------------------------------

def test_the_producer_records_and_rate_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    import core.event_log as el
    monkeypatch.setattr(el, "_LOG", None, raising=False)
    from core.surfaces import poll_health
    poll_health.reset_for_tests()

    assert poll_health.record_poll_error("telegram", TimeoutError("Request timeout error"))
    assert not poll_health.record_poll_error("telegram", TimeoutError("again"))

    from core.event_kinds import SURFACE_POLL_ERROR
    from core.event_log import TelemetryEventLog
    rows = TelemetryEventLog(str(tmp_path / "t.db")).query(kind=SURFACE_POLL_ERROR)
    assert len(rows) == 1, "a 1s retry loop must not become one row per retry"
    assert rows[0]["source"] == "telegram"
    assert "TimeoutError" in rows[0]["attrs"]["error"]


def test_the_rate_limit_is_per_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    import core.event_log as el
    monkeypatch.setattr(el, "_LOG", None, raising=False)
    from core.surfaces import poll_health
    poll_health.reset_for_tests()
    assert poll_health.record_poll_error("telegram", OSError("reset"))
    assert poll_health.record_poll_error("email", OSError("reset")), \
        "one surface's turbulence must not hide another's"


def test_the_producer_never_raises_into_the_poll_loop(monkeypatch):
    from core.surfaces import poll_health
    poll_health.reset_for_tests()
    monkeypatch.setattr("core.event_log.event_log_enabled",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert poll_health.record_poll_error("telegram", ValueError("x")) is False


def test_the_harness_calls_the_shared_recorder():
    """The poll loop must not carry its own copy of this (it is on the status
    silence ratchet, and the IMAP fetcher needs the same answer)."""
    import inspect
    import surfaces.telegram.harness as h
    src = inspect.getsource(h)
    assert "record_poll_error(\"telegram\", e)" in src
    assert "from core.surfaces.poll_health import record_poll_error" in src
