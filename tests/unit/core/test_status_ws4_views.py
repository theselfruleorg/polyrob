"""080 WS-4 views: what runs next, the IDLE headline, blocked reasons, the
spend window, honest reads, an empty /run registry and the per-run cost."""
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from core.config_policy import AutonomyConfig

OWNER = "rob"


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", d)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", os.path.join(d, "telemetry_events.db"))
    return d


def _gates(monkeypatch, cron: bool, goals: bool):
    monkeypatch.setattr(AutonomyConfig, "cron_enabled", staticmethod(lambda: cron))
    monkeypatch.setattr(AutonomyConfig, "goals_enabled", staticmethod(lambda: goals))


def _jobs(d):
    from cron.jobs import CronJob, CronJobStore
    store = CronJobStore(os.path.join(d, "cron.db"))
    now = datetime.now(timezone.utc)
    store.add(CronJob(id="j2", task="Inbox triage\nlong body", schedule_spec="1h", user_id=OWNER,
                      next_run_at=now + timedelta(hours=2), enabled=True, created_at=now))
    store.add(CronJob(id="j1", task="Release watch", schedule_spec="30m", user_id=OWNER,
                      next_run_at=now + timedelta(minutes=30), enabled=True, created_at=now))
    store.add(CronJob(id="j3", task="Disabled job", schedule_spec="30m", user_id=OWNER,
                      next_run_at=now + timedelta(minutes=5), enabled=False, created_at=now))


def _board(d):
    from agents.task.goals.board import GoalBoard
    return GoalBoard(os.path.join(d, "goals.db"))


def test_next_lists_jobs_and_ready_goals_by_name(home, monkeypatch):
    from core.status_snapshot import build_status_snapshot
    _gates(monkeypatch, cron=True, goals=True)
    _jobs(home)
    b = _board(home)
    b.create(user_id=OWNER, title="Low goal", status="ready", priority=1)
    b.create(user_id=OWNER, title="High goal", status="ready", priority=9)
    sec = build_status_snapshot(OWNER, data_dir=home, include_money=False).section("loops")
    names = [i["name"] for i in sec.data["next"]["items"]]
    assert names == ["High goal", "Low goal", "Release watch", "Inbox triage"]
    line = next(ln for ln in sec.lines if ln.startswith("next: "))
    assert "Release watch (job," in line and "Disabled job" not in line


def test_next_says_autonomy_off_and_headline_is_not_running(home, monkeypatch):
    from core.status_render import pause_headline, pause_headline_from
    from core.status_snapshot import build_status_snapshot
    _gates(monkeypatch, cron=False, goals=False)
    _jobs(home)
    snap = build_status_snapshot(OWNER, data_dir=home, include_money=False)
    assert "next: nothing will run: autonomy is off" in snap.section("loops").lines
    for head in (pause_headline(snap), pause_headline_from({})):
        assert "RUNNING" not in head and head.startswith("■ IDLE")


def test_next_names_scheduler_off(home, monkeypatch):
    from core.status_snapshot import build_status_snapshot
    _gates(monkeypatch, cron=False, goals=True)
    _jobs(home)
    sec = build_status_snapshot(OWNER, data_dir=home, include_money=False).section("loops")
    line = next(ln for ln in sec.lines if ln.startswith("next: "))
    assert "scheduler is off" in line


def test_cron_ledger_says_job_not_rail(home, monkeypatch):
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import build_status_snapshot
    _gates(monkeypatch, cron=True, goals=True)
    _jobs(home)
    TelemetryEventLog(os.path.join(home, "telemetry_events.db")).record(
        "cron_run", user_id=OWNER, source="cron",
        attrs={"job_id": "j1", "outcome": "cut_by_cap"}, ts=time.time() - 10)
    snap = build_status_snapshot(OWNER, data_dir=home, include_money=False)
    lines = snap.section("loops").lines
    assert any(ln.startswith("job Release watch") for ln in lines)
    assert not any(ln.startswith("rail ") for ln in lines)
    assert any(h.text.startswith("job run cut") for h in snap.health)


def test_blocked_goal_names_its_reason_and_paused_is_not_health(home, monkeypatch):
    from core.status_snapshot import build_status_snapshot
    _gates(monkeypatch, cron=True, goals=True)
    b = _board(home)
    g = b.create(user_id=OWNER, title="Trade once granted")
    p = b.create(user_id=OWNER, title="Owner held this")
    with sqlite3.connect(os.path.join(home, "goals.db")) as c:
        c.execute("UPDATE goals SET status='blocked', last_failure_error=? WHERE id=?",
                  ("agent declared BLOCKED: grant defi_trade", g.id))
        c.execute("UPDATE goals SET status='paused' WHERE id=?", (p.id,))
    snap = build_status_snapshot(OWNER, data_dir=home, include_money=False)
    item = next(h for h in snap.health if h.key == f"blocked:{g.id}")
    assert "it needs something from you" in item.text and "grant defi_trade" in item.text
    assert not any(p.id in h.key for h in snap.health)
    assert "1 paused by you" in snap.section("work").lines[0]


def test_no_sink_delivery_is_a_health_item(home, monkeypatch):
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import build_status_snapshot
    TelemetryEventLog(os.path.join(home, "telemetry_events.db")).record(
        "user_delivery", user_id=OWNER, source="cron",
        attrs={"outcome": "no_sink"}, ts=time.time() - 10)
    snap = build_status_snapshot(OWNER, data_dir=home, include_money=False)
    assert any(h.key == "delivery_no_sink" for h in snap.health)


def _usage_db(home, rows):
    os.makedirs(os.path.join(home, "database"), exist_ok=True)
    db = os.path.join(home, "database", "bot.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE usage_records (id INTEGER PRIMARY KEY, user_id TEXT, "
                  "session_id TEXT, input_tokens INT, cached_tokens INT, output_tokens INT, "
                  "api_cost_usd REAL, metadata TEXT, timestamp TEXT)")
        for sid, usd, ts in rows:
            c.execute("INSERT INTO usage_records (user_id, session_id, input_tokens, "
                      "cached_tokens, output_tokens, api_cost_usd, metadata, timestamp) "
                      "VALUES (?,?,0,0,0,?,'{}',?)", (OWNER, sid, usd, ts))
    return db


def test_money_runtime_line_reuses_economics_and_names_its_window(home, monkeypatch):
    from core.status_money import _money_section
    monkeypatch.delenv("DB_PATH", raising=False)
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    _usage_db(home, [("s1", 0.5, fresh), ("s1", 0.25, fresh), ("s2", 9.0, old)])
    led = {"runtime": {"spend_window_usd": 99.0, "spend_total_usd": 9.75},
           "treasury": {}, "window_days": 1}
    line = _money_section(OWNER, led, home).lines[0]
    assert "$0.75 last 24h" in line            # the economics read, not the ledger's 99
    led7 = dict(led, window_days=7)
    line7 = _money_section(OWNER, led7, os.path.join(home, "nowhere")).lines[0]
    assert "$99.00 last 7 days" in line7 and "24h" not in line7


def test_run_cost_usd(home, monkeypatch):
    from core.status_economics import run_cost_usd
    monkeypatch.delenv("DB_PATH", raising=False)
    assert run_cost_usd("s1", home) is None              # no ledger: unknown, not 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _usage_db(home, [("s1", 0.5, now), ("s1", 0.25, now), ("s2", 1.0, now)])
    assert run_cost_usd("s1", home) == 0.75
    assert run_cost_usd("nope", home) == 0.0
    assert run_cost_usd("", home) is None


def test_run_reads_missing_registry_as_empty(home):
    from core.run_control import NOTHING_RUNS, run_reply
    from core.status_live import build_live_status
    live = build_live_status(OWNER, home, os.path.join(home, "sessions"))
    assert live["sessions"] == []
    import asyncio
    assert asyncio.run(run_reply(OWNER, home, os.path.join(home, "sessions"), [])) == NOTHING_RUNS


def test_recap_names_a_failed_read(monkeypatch, tmp_path):
    from core import recap

    def boom(*a, **k):
        raise RuntimeError("memory down")

    monkeypatch.setattr(recap, "_episodes", boom)
    monkeypatch.setattr(recap, "_ledger", lambda *a: {})
    monkeypatch.setattr(recap, "_authored", lambda *a: [])
    monkeypatch.setattr(recap, "_events", lambda *a: [])
    entries = recap.build_recap(OWNER, str(tmp_path), "24h")
    assert entries[0].kind == recap.KIND_UNREADABLE and "episodes" in entries[0].text
    out = recap.format_recap_markdown(entries, "24h")
    assert "Nothing to report" not in out and "could not read" in out


def test_journey_did_reads_done_goals_and_names_failures(monkeypatch, tmp_path):
    from cli.ui.commands.h_journey import render_journey
    from core import recap
    b = _board(str(tmp_path))
    g = b.create(user_id=OWNER, title="Ship the release notes")
    with sqlite3.connect(os.path.join(str(tmp_path), "goals.db")) as c:
        c.execute("UPDATE goals SET status='done', completed_at=? WHERE id=?",
                  (time.time() - 60, g.id))
    monkeypatch.setattr(recap, "_episodes", lambda *a: [])
    monkeypatch.setattr(recap, "_events", lambda *a: [])
    monkeypatch.setattr(recap, "_authored", lambda *a: [])

    def boom(*a):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(recap, "_ledger", boom)
    text = render_journey(user_id=OWNER, since_label="24h", data_dir=str(tmp_path))
    assert "goal done: Ship the release notes" in text
    assert "could not read the ledger" in text
    assert "no money activity recorded yet" not in text


def test_digest_names_read_failures_and_says_last_24h(monkeypatch):
    import asyncio
    import cron.digest as digest

    def boom(*a):
        raise RuntimeError("down")

    monkeypatch.setattr(digest, "_ledger", lambda *a: {})
    monkeypatch.setattr(digest, "_event_aggregate", boom)
    monkeypatch.setattr(digest, "_open_asks", boom)
    monkeypatch.setattr(digest, "_episodes", lambda *a: [{"kind": "goal"}] * 20)
    monkeypatch.setattr(digest, "_health_lines", lambda *a: [])
    monkeypatch.setattr(digest, "_outward", lambda *a: "none")
    monkeypatch.setattr(digest, "_missed_lines", lambda *a: [])
    text = asyncio.run(digest.compose_digest(OWNER, days=1))
    assert "Daily digest — last 24 h" in text
    assert "20+ session(s)" in text and "? goal event(s)" in text
    assert "Could not read: event log" in text and "open asks" in text
    assert "Pending approvals: could not read" in text
    assert "Pending approvals: none" not in text
