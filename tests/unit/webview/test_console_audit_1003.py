"""Regression tests for the 2026-10-03 interface + surface audit — the console
reads.

The rule they share (AGENTS.md): an unreadable store is not an empty one.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _pages_client():
    import webview.pages as pages
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


# --------------------------------------------------------------------------- #
# WV2 — an unreadable SOUL/SELF doc is named, never "nothing written yet"
# --------------------------------------------------------------------------- #

def test_identity_unreadable_soul_is_named_not_empty(monkeypatch, tmp_path):
    client, pages = _pages_client()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "identity.md").write_bytes(b"\xff\xfe not utf-8 \xff")
    body = client.get("/api/webgate/identity").json()
    assert body["soul"] is None
    assert body["soul_error"], "an unreadable SOUL doc must carry its reason"
    assert str(tmp_path) not in body["soul_error"]  # WR8: no host path
    assert body["self_error"] is None


def test_identity_unreadable_self_is_named_not_empty(monkeypatch, tmp_path):
    client, pages = _pages_client()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from core.instance import self_tier_root
    from webview import webgate
    root = self_tier_root(tmp_path, webgate.local_owner_id(), pages.resolve_instance_id())
    root.mkdir(parents=True)
    (root / "self.md").write_bytes(b"\xff\xfe\xff")
    body = client.get("/api/webgate/identity").json()
    assert body["self"] is None
    assert body["self_error"]


def test_identity_absent_docs_are_empty_without_error(monkeypatch, tmp_path):
    client, _ = _pages_client()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    body = client.get("/api/webgate/identity").json()
    assert body["soul"] is None and body["self"] is None
    assert body["soul_error"] is None and body["self_error"] is None


# --------------------------------------------------------------------------- #
# WV4 — a memory backend that could not be BUILT is named, never "not configured"
# --------------------------------------------------------------------------- #

def _broken_memory(monkeypatch):
    import webview.knowledge as knowledge
    import webview.pages as pages
    broken = lambda: (None, "OperationalError: database disk image is malformed")
    monkeypatch.setattr(pages, "_memory_provider_status", broken)
    monkeypatch.setattr(knowledge, "_memory_provider_status", broken)


def test_memory_search_broken_backend_is_named(monkeypatch):
    client, _ = _pages_client()
    _broken_memory(monkeypatch)
    body = client.get("/api/webgate/memory").json()
    assert body["items"] is None and body["count"] is None
    assert "malformed" in body["error"]


def test_knowledge_lists_broken_backend_is_named(monkeypatch):
    import webview.knowledge as knowledge
    _broken_memory(monkeypatch)
    app = FastAPI()
    app.include_router(knowledge.router)
    client = TestClient(app)
    for path in ("/api/webgate/knowledge/notes", "/api/webgate/knowledge/episodes",
                 "/api/webgate/knowledge/kb"):
        body = client.get(path).json()
        assert body["items"] is None, path
        assert body["error"] and "malformed" in body["error"], path
    body = client.get("/api/webgate/knowledge/note/1").json()
    assert body["note"] is None and "malformed" in body["error"]


def test_agent_memory_body_broken_backend_is_named(monkeypatch):
    import asyncio
    from webview.pages_new import _memory_search_body
    _broken_memory(monkeypatch)
    body = asyncio.run(_memory_search_body("rob", "", 10))
    assert body["unavailable"] is False
    assert body["recall"] is None and "malformed" in body["recall_error"]
    assert body["notes"] is None and "malformed" in body["notes_error"]


# --------------------------------------------------------------------------- #
# WV3 — Work › Log merges the cold window with the live hub, always
# --------------------------------------------------------------------------- #

def test_log_keeps_history_after_the_first_live_event(monkeypatch):
    import webview.activity as activity
    import webview.worklog_api as worklog
    old = {"id": "telemetry:1", "ts": 10.0, "kind": "goal_created", "user_id": ""}
    live = {"id": "telemetry:2", "ts": 20.0, "kind": "goal_claimed", "user_id": ""}
    hub = activity.ActivityHub()
    hub.record(dict(live))
    monkeypatch.setattr(activity, "_hub", hub)
    monkeypatch.setattr(activity, "_cold_backfill", lambda limit: [dict(old), dict(live)])
    events = worklog._recent_events(50)
    assert [e["id"] for e in events] == ["telemetry:1", "telemetry:2"]  # deduped, oldest first


# --------------------------------------------------------------------------- #
# WR2 — the cold feed seed picks the newest files by mtime, not by name
# --------------------------------------------------------------------------- #

def test_cold_feed_seed_orders_by_mtime(tmp_path):
    import os
    from webview.activity import _newest_feed_names
    (tmp_path / "session_start_old.json").write_text("{}")
    (tmp_path / "000123.json").write_text("{}")
    os.utime(tmp_path / "session_start_old.json", (100, 100))
    os.utime(tmp_path / "000123.json", (200, 200))
    assert _newest_feed_names(str(tmp_path), 1) == ["000123.json"]


# --------------------------------------------------------------------------- #
# WR3 — an unreadable store at prime never replays history; a cold read raises
# --------------------------------------------------------------------------- #

def _telemetry_db(path, rows=3):
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE telemetry_events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT)")
    for i in range(rows):
        con.execute("INSERT INTO telemetry_events (ts, kind) VALUES (?, 'k')", (float(i),))
    con.commit()
    con.close()


def test_tail_unreadable_at_prime_does_not_replay_the_table(tmp_path):
    from webview.activity import SqliteTail
    db = tmp_path / "telemetry_events.db"
    db.write_bytes(b"this is not a sqlite database" * 64)
    tail = SqliteTail(str(db), "telemetry_events")
    tail.prime()
    assert tail.primed is False
    assert tail.poll() == []
    db.unlink()
    _telemetry_db(str(db), rows=3)
    assert tail.poll() == [], "the late prime must start at MAX(id), not replay rows 1-3"
    assert tail.cursor == 3


def test_tail_recent_raises_on_an_unreadable_store(tmp_path):
    import pytest
    from webview.activity import SqliteTail
    db = tmp_path / "telemetry_events.db"
    db.write_bytes(b"this is not a sqlite database" * 64)
    with pytest.raises(Exception):
        SqliteTail(str(db), "telemetry_events").recent(10)
    # absent store / absent table stay the genuine empty state
    assert SqliteTail(str(tmp_path / "nope.db"), "telemetry_events").recent(10) == []
    _telemetry_db(str(tmp_path / "other.db"), rows=0)
    assert SqliteTail(str(tmp_path / "other.db"), "skill_install_audit").recent(10) == []


def test_cold_backfill_names_the_unreadable_source(monkeypatch, tmp_path):
    import pytest
    import webview.activity as activity
    db = tmp_path / "goals.db"
    db.write_bytes(b"this is not a sqlite database" * 64)
    monkeypatch.setattr(activity, "activity_db_sources",
                        lambda: [("goal", activity.goal_events_tail(str(db)))])
    with pytest.raises(activity.SourceUnreadable, match="goal"):
        activity._cold_backfill(10)


# --------------------------------------------------------------------------- #
# WR6 — untenanted rows are not every tenant's on a multitenant console
# --------------------------------------------------------------------------- #

def test_untenanted_rows_belong_only_to_the_instance_owner_in_multitenant(monkeypatch):
    import webview.webgate as webgate
    import webview.worklog_api as worklog
    monkeypatch.setattr(webgate, "is_multitenant", lambda: True)
    monkeypatch.setattr(webgate, "local_owner_id", lambda: "owner-1")
    assert worklog._own_untenanted_rows("owner-1") is True
    assert worklog._own_untenanted_rows("tenant-2") is False
    monkeypatch.setattr(webgate, "is_multitenant", lambda: False)
    assert worklog._own_untenanted_rows("anyone") is True


# --------------------------------------------------------------------------- #
# WR8 — a read error never prints the host's file layout
# --------------------------------------------------------------------------- #

def test_safe_reason_drops_absolute_paths():
    from webview.pages import _safe_reason
    exc = FileNotFoundError(2, "No such file", "/srv/polyrob/data/goals.db")
    reason = _safe_reason(exc)
    assert "/srv" not in reason and "goals.db" in reason
    assert _safe_reason(ValueError("ratio 3/4")) == "ValueError: ratio 3/4"


def test_goals_read_error_has_no_path(monkeypatch):
    client, pages = _pages_client()
    monkeypatch.setattr(pages.AutonomyConfig, "goals_enabled", staticmethod(lambda: True))

    class Boom:
        def __init__(self, *a, **k):
            raise OSError("unable to open /var/lib/polyrob/goals.db")

    monkeypatch.setattr(pages, "GoalBoard", Boom)
    body = client.get("/api/webgate/goals").json()
    assert body["error"] and "/var/lib" not in body["error"]


# --------------------------------------------------------------------------- #
# WR5 / WR9 / WR10 / WR12 — pages.py writes say what really happened
# --------------------------------------------------------------------------- #

def _owner_client(monkeypatch, tmp_path, user_id="u1"):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


def test_pref_list_addition_that_did_not_persist_is_not_success(monkeypatch, tmp_path):
    from core.prefs import write_preference
    client, pages = _owner_client(monkeypatch, tmp_path)
    ok, err = write_preference(tmp_path, "u1", "approvals.require", ["x402_request", "git_push"])
    assert ok, err
    monkeypatch.setattr(pages, "write_preference", lambda *a, **k: (False, "disk full"))
    r = client.patch("/api/webgate/preferences",
                     json={"key": "approvals.require", "value": ["x402_request", "email_send"],
                           "confirm": True})
    body = r.json()
    assert body["ok"] is False and body["error"] == "disk full"
    assert body["applied_additions"] == []


def test_pending_decide_on_a_refusing_store_is_not_a_500(monkeypatch, tmp_path):
    import sqlite3
    client, pages = _owner_client(monkeypatch, tmp_path)

    def _locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(pages, "_decide_pending", _locked)
    for verb in ("promote", "reject"):
        r = client.post(f"/api/webgate/pending/ask/g1/{verb}")
        assert r.status_code == 200, verb
        body = r.json()
        assert body["ok"] is False and "locked" in body["error"]


def test_config_write_non_object_body_is_400(monkeypatch, tmp_path):
    from webview import webgate
    client, _ = _owner_client(monkeypatch, tmp_path)
    monkeypatch.setattr(webgate, "posture", lambda: "local")
    monkeypatch.setattr(webgate, "read_only", lambda: False)
    monkeypatch.chdir(tmp_path)
    r = client.patch("/api/webgate/config/goals.daily_quota", json=["3"])
    assert r.status_code == 400
