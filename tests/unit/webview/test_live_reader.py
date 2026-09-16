"""``/api/webgate/live`` — what is in progress RIGHT NOW (2026-09-16 audit, B1).

Work › Now used to count only running GOALS and background delegations, so a
console beside an agent whose whole day is CRON runs said "Rob is not running
anything right now." while a run was mid-flight. This reader names every live
actor the stores can prove — running goals, running cron jobs (the CAS in
``cron/jobs.py`` marks a job ``running`` for the length of its run) and the live
sessions the shared session registry knows about — tenant-scoped, with every
unreadable source NAMED rather than folded into a confident empty list.
"""
import json
import os
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import webview.pages_new as mod


def _stores(tmp_path, *, pid=None):
    data = tmp_path / "data"
    data.mkdir()
    con = sqlite3.connect(data / "goals.db")
    con.execute("""CREATE TABLE goals (id TEXT PRIMARY KEY, user_id TEXT, title TEXT,
                   kind TEXT DEFAULT 'goal', status TEXT, started_at REAL, session_id TEXT)""")
    con.execute("INSERT INTO goals VALUES ('g1','rob','Engage the den','goal','running',1000.0,'s-goal')")
    con.execute("INSERT INTO goals VALUES ('g2','rob','Queued thing','goal','ready',NULL,NULL)")
    con.execute("INSERT INTO goals VALUES ('g3','other','Not mine','goal','running',1000.0,NULL)")
    con.commit(); con.close()
    con = sqlite3.connect(data / "cron.db")
    con.execute("""CREATE TABLE cron_jobs (id TEXT PRIMARY KEY, task TEXT, user_id TEXT,
                   status TEXT, last_run_at TEXT)""")
    con.execute("INSERT INTO cron_jobs VALUES ('c1','SCOUT-ENTRY run (24/7)' || char(10) || 'second line','rob','running','2026-09-16T03:00:00')")
    con.execute("INSERT INTO cron_jobs VALUES ('c2','Digest','rob','scheduled',NULL)")
    con.execute("INSERT INTO cron_jobs VALUES ('c3','Theirs','other','running',NULL)")
    con.commit(); con.close()
    con = sqlite3.connect(data / "session_registry.db")
    con.execute("""CREATE TABLE active_sessions (session_id TEXT PRIMARY KEY, worker_pid INTEGER,
                   status TEXT, params TEXT, last_seen_at TEXT, created_at TEXT, owner_boot_id TEXT)""")
    me = pid if pid is not None else os.getpid()
    con.execute("INSERT INTO active_sessions VALUES ('s-live', ?, 'active', '{}', 'x', 'y', 'b')", (me,))
    con.execute("INSERT INTO active_sessions VALUES ('s-theirs', ?, 'active', '{}', 'x', 'y', 'b')", (me,))
    con.commit(); con.close()
    root = tmp_path / "sessions"
    for user, sid, status, task in (("rob", "s-live", "running", "EXIT-MONITOR run"),
                                    ("other", "s-theirs", "running", "not mine")):
        d = root / user / sid
        d.mkdir(parents=True)
        (d / "task.json").write_text(json.dumps({"task": task, "creator": "cron"}))
        (d / "status.json").write_text(json.dumps({"status": status}))
    return str(data), str(root)


def test_live_body_names_every_running_actor_for_the_tenant(tmp_path, monkeypatch):
    data, root = _stores(tmp_path)
    body = mod._live_body("rob", data, root)
    assert [g["id"] for g in body["goals"]] == ["g1"]
    assert body["goals"][0]["title"] == "Engage the den" and body["goals"][0]["since"] == 1000.0
    assert [c["id"] for c in body["cron"]] == ["c1"]
    assert body["cron"][0]["task"] == "SCOUT-ENTRY run (24/7)"  # one line, this tenant's
    assert isinstance(body["cron"][0]["since"], float)
    assert [s["session_id"] for s in body["sessions"]] == ["s-live"]
    assert body["sessions"][0]["task"] == "EXIT-MONITOR run"
    assert body["sessions"][0]["status"] == "running"
    assert body["count"] == 3
    assert body["unreadable"] == {}


def test_live_body_names_a_missing_store_never_a_confident_zero(tmp_path):
    data = tmp_path / "empty"
    data.mkdir()
    body = mod._live_body("rob", str(data), str(tmp_path / "nowhere"))
    assert body["goals"] is None and body["cron"] is None and body["sessions"] is None
    assert set(body["unreadable"]) == {"goals", "cron", "sessions"}
    assert body["count"] is None


def test_live_body_drops_a_dead_owner_pid(tmp_path):
    data, root = _stores(tmp_path, pid=2**22 - 7)  # almost certainly not alive
    body = mod._live_body("rob", data, root)
    assert body["sessions"] == []


def test_live_route_scopes_to_the_effective_tenant(tmp_path, monkeypatch):
    data, root = _stores(tmp_path)
    monkeypatch.setattr("webview.pages._effective_user_id", lambda request: "rob")
    monkeypatch.setattr(mod.webgate, "data_dir", lambda: data)
    monkeypatch.setattr(mod, "_sessions_root", lambda: root)
    app = FastAPI()
    app.include_router(mod.api_router)
    resp = TestClient(app).get("/api/webgate/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3 and body["user_id"] == "rob"


def test_live_count_feeds_the_head_line(tmp_path, monkeypatch):
    data, root = _stores(tmp_path)
    monkeypatch.setattr(mod.webgate, "data_dir", lambda: data)
    monkeypatch.setattr(mod, "_sessions_root", lambda: root)
    monkeypatch.setattr(mod, "_tenant", lambda request: ("rob", None))
    assert mod._live_count(object()) == 3
    # A tenant the shell cannot name is 0 in-progress, never a crash.
    monkeypatch.setattr(mod, "_tenant", lambda request: (None, "unbound"))
    assert mod._live_count(object()) == 0


def test_head_line_says_how_many_things_are_in_progress(monkeypatch):
    from core.surfaces.inbox import compose
    from webview.copy import t
    monkeypatch.setattr(mod, "_pause_headline", lambda: t("shell.state.running"))
    monkeypatch.setattr(mod, "_inbox_summary", lambda request: compose([], {"asks": "ok"}))
    monkeypatch.setattr(mod, "_live_count", lambda request: 2)
    app = FastAPI()
    app.include_router(mod.router)
    html = TestClient(app).get("/work").text
    assert "Rob is running, 2 in progress." in html
    monkeypatch.setattr(mod, "_live_count", lambda request: 0)
    assert "Rob is running. Nothing needs you." in TestClient(app).get("/work").text


def test_head_route_renders_the_same_two_lines_as_the_shell(monkeypatch):
    from core.surfaces.inbox import compose
    from webview.copy import t
    monkeypatch.setattr(mod, "_pause_headline", lambda: t("shell.state.running"))
    monkeypatch.setattr(mod, "_inbox_summary", lambda request: compose([], {"asks": "ok"}))
    monkeypatch.setattr(mod, "_live_count", lambda request: 1)
    app = FastAPI()
    app.include_router(mod.api_router)
    body = TestClient(app).get("/api/webgate/head").json()
    assert body == {"headline": "Rob is running, 1 in progress.",
                    "waiting_line": "Nothing needs you.",
                    "badge": "", "aria": "", "partial": False}


def test_shell_loads_the_live_module(monkeypatch):
    from core.surfaces.inbox import compose
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_inbox_summary", lambda request: compose([], {"asks": "ok"}))
    monkeypatch.setattr(mod, "_live_count", lambda request: 0)
    app = FastAPI()
    app.include_router(mod.router)
    html = TestClient(app).get("/inbox").text
    assert '/static/app/live.js' in html


def test_shell_offers_logout_only_where_a_login_exists(monkeypatch):
    from core.surfaces.inbox import compose
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_inbox_summary", lambda request: compose([], {"asks": "ok"}))
    monkeypatch.setattr(mod, "_live_count", lambda request: 0)
    app = FastAPI()
    app.include_router(mod.router)
    monkeypatch.setattr(mod, "_show_logout", lambda: True)
    html = TestClient(app).get("/inbox").text
    assert 'href="/logout"' in html and "Log out" in html
    monkeypatch.setattr(mod, "_show_logout", lambda: False)
    assert 'href="/logout"' not in TestClient(app).get("/inbox").text


def test_live_metadata_failure_is_named(tmp_path):
    from pathlib import Path
    data, root = _stores(tmp_path)
    (Path(root) / 'rob' / 's-live' / 'status.json').write_text('broken')
    body = mod._live_body('rob', data, root)
    assert body['sessions'][0]['status'] is None
    assert 'session:s-live:status' in body['unreadable']
    assert body['count_unit'] == 'actors'
