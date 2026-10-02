"""045 lane 4: volume and spend per chat, limiter trips, and the scan verdict
in the ONE security rollup. GROUP BY reads; an absent store is a named
reason, never a zero."""
import json
import sqlite3
import time

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    monkeypatch.delenv("DB_PATH", raising=False)
    import core.event_log as el
    el._INSTANCES.clear()
    log = el.get_event_log()
    from core import event_kinds as ek
    for _ in range(4):
        log.record(ek.INBOUND_ROUTED, user_id="u1", source="perimeter",
                   attrs={"surface": "telegram", "chat_id": "-100", "sender": "7"})
    log.record(ek.ACCESS_DENIED, user_id="u1", source="perimeter",
               attrs={"surface": "telegram", "chat_id": "55", "sender": "9"})
    for key in ("1.1.1.1", "1.1.1.1", "2.2.2.2"):
        log.record(ek.RATE_LIMITED, user_id="u1", source="rate_limit",
                   attrs={"limiter": "webview_connect", "key": key})
    log.record(ek.RATE_LIMITED, user_id="other", source="rate_limit",
               attrs={"limiter": "api_minute", "key": "x"})
    yield tmp_path
    el._INSTANCES.clear()


def _seed_spend(home):
    s = sqlite3.connect(str(home / "surfaces.db"))
    s.execute("CREATE TABLE session_chat_map (session_key TEXT PRIMARY KEY, "
              "session_id TEXT NOT NULL, user_id TEXT, surface_id TEXT, chat_id TEXT, "
              "owner_pid INTEGER, updated_at REAL)")
    s.execute("INSERT INTO session_chat_map VALUES ('k1','sA','u1','telegram','-100',0,0)")
    s.execute("INSERT INTO session_chat_map VALUES ('k2','sB','u1','telegram','55',0,0)")
    s.commit(); s.close()
    (home / "database").mkdir()
    b = sqlite3.connect(str(home / "database" / "bot.db"))
    b.execute("CREATE TABLE usage_records (id INTEGER PRIMARY KEY, user_id TEXT, "
              "session_id TEXT, api_cost_usd REAL, timestamp TEXT)")
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    b.executemany("INSERT INTO usage_records (user_id, session_id, api_cost_usd, timestamp) "
                  "VALUES (?,?,?,?)",
                  [("u1", "sA", 0.5, now), ("u1", "sA", 0.25, now),
                   ("u1", "sB", 0.1, now), ("u1", "sB", 9.0, "2000-01-01 00:00:00"),
                   ("u1", "unbound", 3.0, now)])
    b.commit(); b.close()


def _rollup(home):
    from core.security_digest import build_security_rollup
    return build_security_rollup("u1", data_dir=str(home),
                                 since_ts=time.time() - 3600, window_sec=3600)


def test_trips_counted_and_ranked_tenant_scoped(home):
    r = _rollup(home)
    assert r.rate_limited == 3
    assert r.top_limiters == [("webview_connect", 3)]
    assert r.top_limited_keys[0] == ("1.1.1.1", 2)


def test_chats_ranked_by_volume(home):
    r = _rollup(home)
    assert r.top_chats_by_volume[0] == ("telegram:-100", 4)
    assert ("telegram:55", 1) in r.top_chats_by_volume


def test_chats_ranked_by_spend_in_window_only(home):
    _seed_spend(home)
    r = _rollup(home)
    assert r.spend_unavailable == ""
    assert r.top_chats_by_spend == [("telegram:-100", 0.75), ("telegram:55", 0.1)]


def test_missing_spend_store_is_named_not_zero(home):
    r = _rollup(home)
    assert "surfaces.db" in r.spend_unavailable
    assert r.top_chats_by_spend == []
    assert r.inbound == 4   # the other lanes stay readable


def test_scan_not_run_when_no_verdict(home):
    assert _rollup(home).scan == "not run"


@pytest.mark.parametrize("payload,expect", [
    ({"verdict": "clean"}, "clean"),
    ({"verdict": "findings", "findings": 3}, "findings 3"),
    ({"verdict": "incomplete", "not_run": ["pip_audit"]}, "incomplete (pip_audit not run)"),
])
def test_scan_verdict_phrases(home, payload, expect):
    from core.security_digest import scan_verdict_path
    p = scan_verdict_path(str(home))
    (home / "ops").mkdir()
    open(p, "w").write(json.dumps({"epoch": time.time(), "ts": "t", **payload}))
    assert _rollup(home).scan == expect


def test_stale_and_unreadable_verdicts(home):
    from core.security_digest import read_scan_verdict, scan_verdict_path
    (home / "ops").mkdir()
    p = scan_verdict_path(str(home))
    open(p, "w").write(json.dumps({"epoch": 0, "ts": "1970", "verdict": "clean"}))
    assert read_scan_verdict(str(home), time.time()).startswith("stale")
    open(p, "w").write("{not json")
    assert read_scan_verdict(str(home), time.time()).startswith("unavailable(")
