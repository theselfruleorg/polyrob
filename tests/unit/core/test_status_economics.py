"""057 WS-I: the economics section reads usage_records and never guesses."""
import json
import os
import sqlite3
import time

import pytest

from core.status_economics import (bot_db_path, compute_economics, economics_section,
                                   runway_health)
from core.status_snapshot import (SEVERITY_CRIT, SEVERITY_WARN, STATE_UNAVAILABLE,
                                  _guarded, build_status_snapshot)


def _usage_db(tmp_path, rows):
    d = tmp_path / "database"
    d.mkdir(exist_ok=True)
    db = d / "bot.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE usage_records (id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL, session_id TEXT NOT NULL, resource_type TEXT NOT NULL,
        cost INTEGER NOT NULL, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
        cached_tokens INTEGER DEFAULT 0, api_cost_usd REAL DEFAULT 0.0, markup_multiplier REAL DEFAULT 1.0,
        request_id TEXT, metadata TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    for r in rows:
        con.execute("INSERT INTO usage_records(user_id, session_id, resource_type, cost, input_tokens, "
                    "output_tokens, cached_tokens, api_cost_usd, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["user"], r["sid"], "llm", 1, r["in"], r["out"], r["cached"], r["usd"],
                     json.dumps(r.get("md") or {})))
    con.commit()
    con.close()
    return str(db)


def _rows():
    return [
        {"user": "rob", "sid": "s1", "in": 100_000, "cached": 90_000, "out": 1_000, "usd": 0.02,
         "md": {"duration_seconds": 10.0}},
        {"user": "rob", "sid": "s1", "in": 120_000, "cached": 100_000, "out": 4_000, "usd": 0.03,
         "md": {"duration_seconds": 80.0, "finish_reason": "length"}},
        {"user": "rob", "sid": "s2", "in": 90_000, "cached": 0, "out": 500, "usd": 0.05,
         "md": {"duration_seconds": 30.0, "error": "asyncio TimeoutError"}},
        {"user": "other", "sid": "x", "in": 5_000_000, "cached": 0, "out": 1, "usd": 99.0},
    ]


def test_compute_is_pure_and_honest():
    e = compute_economics([], 86400)
    assert e["calls"] == 0 and e["cost_per_day_usd"] is None and e["latency_p50"] is None
    rows = [{"session_id": r["sid"], "input_tokens": r["in"], "cached_tokens": r["cached"],
             "output_tokens": r["out"], "api_cost_usd": r["usd"], "metadata": json.dumps(r.get("md") or {})}
            for r in _rows() if r["user"] == "rob"]
    e = compute_economics(rows, 86400)
    assert e["calls"] == 3 and e["sessions"] == 2
    assert round(e["cache_ratio"], 3) == round(190_000 / 310_000, 3)
    assert e["first_call_hit_ratio"] == 0.5          # s1 first call hit, s2 first call missed
    assert e["truncated"] == 1 and e["timeouts"] == 1
    assert e["latency_p50"] == 30.0 and e["latency_p90"] == 80.0
    assert round(e["cost_per_day_usd"], 2) == 0.10


def test_section_is_tenant_scoped_and_renders_cache_ratio(tmp_path):
    _usage_db(tmp_path, _rows())
    sec = economics_section("rob", str(tmp_path), now=time.time(), window_sec=86400)
    assert sec.data["calls"] == 3                     # the 'other' tenant's $99 row is invisible
    assert "61% cached" in sec.lines[1]
    assert "truncations 1" in sec.lines[2] and "timeouts 1" in sec.lines[2]


def test_absent_ledger_is_a_fresh_install_and_a_broken_one_is_unavailable(tmp_path):
    sec = _guarded("economics", economics_section, "rob", str(tmp_path), now=time.time())
    assert sec.state != STATE_UNAVAILABLE and "no ledger yet" in sec.lines[0]
    d = tmp_path / "database"; d.mkdir()
    (d / "bot.db").write_text("not a database")
    sec = _guarded("economics", economics_section, "rob", str(tmp_path), now=time.time())
    assert sec.state == STATE_UNAVAILABLE


def test_runway_needs_a_read_balance(tmp_path):
    _usage_db(tmp_path, _rows())
    sec = economics_section("rob", str(tmp_path), now=time.time(), window_sec=86400)
    assert runway_health(sec, None) is None and not sec.health
    item = runway_health(sec, 0.05)                   # $0.05 at $0.10/day → 12 h
    assert item is not None and item.severity == SEVERITY_CRIT and "12h" in item.text
    sec2 = economics_section("rob", str(tmp_path), now=time.time(), window_sec=86400)
    assert runway_health(sec2, 0.25).severity == SEVERITY_WARN     # 2.5 days
    sec3 = economics_section("rob", str(tmp_path), now=time.time(), window_sec=86400)
    assert runway_health(sec3, 10.0) is None and "runway" in sec3.lines[-1]


def test_read_usage_bounds_a_window_and_lines_are_the_one_render(tmp_path):
    """A post-deploy slice (since→until) must be readable as ONE window so the
    72 h targets are measured on the code that is live, not blended with the 24 h
    before it; and the CLI window prints the SAME three lines the section does."""
    from core.status_economics import economics_lines, read_usage
    db = _usage_db(tmp_path, _rows())
    # same rows, same render: the section (rolling 24 h) and an explicit window agree
    e = compute_economics(read_usage("rob", db, "2000-01-01 00:00:00"), 86400)
    lines = economics_lines(e, "24h")
    sec = economics_section("rob", str(tmp_path), now=time.time(), window_sec=86400)
    assert lines == sec.lines and lines[0].startswith("llm usage 24h: 3 calls")
    con = sqlite3.connect(db)
    con.execute("UPDATE usage_records SET timestamp='2026-09-19 03:00:00' WHERE session_id='s1'")
    con.execute("UPDATE usage_records SET timestamp='2026-09-19 05:00:00' WHERE session_id='s2'")
    con.commit(); con.close()
    assert len(read_usage("rob", db, "2026-09-19 00:00:00")) == 3
    assert len(read_usage("rob", db, "2026-09-19 00:00:00", "2026-09-19 04:00:00")) == 2
    assert len(read_usage("rob", db, "2026-09-19 04:00:00", "2026-09-19 06:00:00")) == 1
    assert economics_lines(compute_economics([], 3600), "03:31→04:31") == \
        ["llm usage: 0 calls in 03:31→04:31"]


def test_truncations_are_counted_from_the_event_log_not_a_finish_reason_nobody_writes(tmp_path, monkeypatch):
    """Prod 2026-09-20 05:05Z: EXIT step 6 returned exactly 8,192 output tokens
    (the cap), the 057 ``llm_output_truncated`` event fired and the retry ran —
    and the Economics line still said ``output truncations 0`` because the usage
    tracker writes no ``finish_reason`` into ``usage_records.metadata``. Three
    calls at the old 16,384 cap the day before were uncounted the same way. The
    event log is the honest source; the metadata path stays as a lower bound."""
    from core.event_log import TelemetryEventLog
    from core.status_economics import truncation_event_count
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    _usage_db(tmp_path, _rows())                       # metadata carries ONE finish_reason=length
    now = time.time()
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("llm_output_truncated", user_id="rob", source="agent.llm_runner",
               attrs={"output_tokens": 8192, "retried": True, "step": 6}, ts=now - 300)
    log.record("llm_output_truncated", user_id="rob", source="agent.llm_runner",
               attrs={"output_tokens": 16384, "retried": False, "step": 3}, ts=now - 200)
    log.record("llm_output_truncated", user_id="other", source="agent.llm_runner",
               attrs={"output_tokens": 16384}, ts=now - 200)              # another tenant
    log.record("llm_output_truncated", user_id="rob", source="agent.llm_runner",
               attrs={"output_tokens": 16384}, ts=now - 90000)             # outside the window
    assert truncation_event_count("rob", str(tmp_path), now - 86400, now) == 2
    sec = economics_section("rob", str(tmp_path), now=now, window_sec=86400)
    assert sec.data["truncated"] == 2 and sec.data["truncated_source"] == "event_log"
    assert "output truncations 2" in sec.lines[2]
    # No event log at all → the metadata lower bound, and the render SAYS so.
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "absent.db"))
    assert truncation_event_count("rob", str(tmp_path), now - 86400, now) is None
    sec2 = economics_section("rob", str(tmp_path), now=now, window_sec=86400)
    assert sec2.data["truncated"] == 1 and sec2.data["truncated_source"] == "metadata"
    assert "output truncations 1 (metadata only)" in sec2.lines[2]


def test_econ_window_script_prints_the_slice(tmp_path, monkeypatch):
    import subprocess, sys
    db = _usage_db(tmp_path, _rows())
    con = sqlite3.connect(db)
    con.execute("UPDATE usage_records SET timestamp='2026-09-19 03:00:00' WHERE session_id='s1'")
    con.execute("UPDATE usage_records SET timestamp='2026-09-19 05:00:00' WHERE session_id='s2'")
    con.commit(); con.close()
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    # `scripts/` is owner-infra and does not ship in the public package, where a
    # bare `pytest` still collects this file. An absent script is 'not this
    # tree's concern', never a failure.
    if not os.path.isfile(os.path.join(root, "scripts", "econ_window.py")):
        pytest.skip("ops scripts absent (public package)")
    env = dict(os.environ, PYTHONPATH=root, POLYROB_DATA_DIR=str(tmp_path))
    env.pop("DB_PATH", None)
    out = subprocess.run([sys.executable, os.path.join(root, "scripts", "econ_window.py"),
                          "--since", "2026-09-19T04:00:00Z", "--user", "rob", "--json"],
                         capture_output=True, text=True, env=env, check=True).stdout
    data = json.loads(out)
    assert data["calls"] == 1 and data["sessions"] == 1 and data["since"] == "2026-09-19 04:00:00"
    text = subprocess.run([sys.executable, os.path.join(root, "scripts", "econ_window.py"),
                           "--since", "2026-09-19 00:00:00", "--until", "2026-09-19 04:00:00", "--user", "rob"],
                          capture_output=True, text=True, env=env, check=True).stdout
    assert "2 calls · 1 sessions" in text and "truncations 1" in text


def test_db_path_honours_DB_PATH(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_PATH", raising=False)
    assert bot_db_path("/d") == os.path.join("/d", "database", "bot.db")
    monkeypatch.setenv("DB_PATH", "/opt/x/bot.db")
    assert bot_db_path("/d") == "/opt/x/bot.db"


def test_snapshot_carries_the_section_in_order(tmp_path, monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    _usage_db(tmp_path, _rows())
    snap = build_status_snapshot("rob", data_dir=str(tmp_path), include_money=False)
    assert list(snap.sections)[-2:] == ["money", "economics"]
    assert snap.sections["economics"].data["calls"] == 3
    snap2 = build_status_snapshot("", data_dir=str(tmp_path), include_money=False)
    assert snap2.sections["economics"].state == STATE_UNAVAILABLE


def test_loops_section_renders_rail_verdicts_with_a_since_clock(tmp_path, monkeypatch):
    """057 WS-F: a standing SMTP/X verdict is read from verdicts.db, aggregated by
    kind, with first_seen as the clock — not from the 24 h telemetry window."""
    import time as _t
    from core import credential_verdicts as cv
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VERDICTS_DB_PATH", str(tmp_path / "verdicts.db"))
    cv._reset_for_tests() if hasattr(cv, "_reset_for_tests") else None
    cv.record_rejection("smtp", "smtp.gmail.com:587:rob", code="535", remedy="set GMAIL_APP_PASSWORD")
    cv.record_rejection("twitter_api", "users", code="402", remedy="top up")
    cv.record_rejection("twitter_api", "post", code="402", remedy="top up")
    cv.record_rejection("missing_key", "perplexity", code="missing_config", remedy="set PERPLEXITY_API_KEY")
    from core.status_snapshot import _rail_verdicts, _rail_verdict_line, SEVERITY_WARN
    vs = _rail_verdicts()
    kinds = {v.kind: v for v in vs}
    assert set(kinds) == {"smtp", "twitter_api", "missing_key"}
    assert kinds["twitter_api"].count == 2                       # aggregated by kind
    line, sev, key = _rail_verdict_line(kinds["twitter_api"])
    assert sev == SEVERITY_WARN and key == "x_api_rejected" and "since" in line and "2 call(s)" in line
    line, sev, key = _rail_verdict_line(kinds["missing_key"])
    assert sev is None and "perplexity" in line                  # a choice, not a fault
    snap = build_status_snapshot("rob", data_dir=str(tmp_path), include_money=False)
    keys = {h.key for h in snap.health}
    assert {"email_auth_rejected", "x_api_rejected"} <= keys and not any(k.startswith("missing_key") for k in keys)
