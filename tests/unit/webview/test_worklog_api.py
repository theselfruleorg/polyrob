"""043 A16 / §5 — GET /api/webgate/log: the classified activity stream.

Read-only, tenant-scoped over the one activity stream ``webview/activity.py``
already builds. Each event is stamped with the class
``core.activity_class.classify`` gives it; the low-signal engine kinds
(``is_diagnostic``) are off unless asked; the exact payload rides only under
``raw``; and a read that FAILS is a named ``unreadable`` answer, never a silent
drop into a confident empty list.

The router is mounted alone on a fresh app (the artifacts/inbox test pattern) so
the console's own posture is what the endpoint sees, not the full server's auth
middleware. The activity read itself is stubbed — this test is the endpoint's
own contract (scope, class, diagnostics, raw, unreadable), not the hub's.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.event_kinds as ek
from core.activity_class import CLIENT_CLASSES, classify, is_diagnostic


@pytest.fixture(autouse=True)
def _no_ambient_owner(monkeypatch):
    """The dev box may carry a real owner in its environment."""
    for name in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                 "POLYROB_LOCAL_OWNER", "SURFACE_SUPER_ADMIN_USER_IDS"):
        monkeypatch.delenv(name, raising=False)


def _client(monkeypatch, posture="local"):
    import webview.webgate as webgate
    import webview.worklog_api as mod
    monkeypatch.setattr(webgate, "posture", lambda: posture)
    monkeypatch.setattr(webgate, "read_only", lambda: True)  # silence unbound warn
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def _event(kind, *, user_id, summary="did a thing", ts=1000.0, payload=None):
    return {
        "id": f"{kind}:{ts}",
        "ts": ts,
        "source": "feed",
        "user_id": user_id,
        "session_id": "s1",
        "kind": kind,
        "summary": summary,
        "payload": payload if payload is not None else {"kind": kind, "n": 1},
    }


def _stub_events(monkeypatch, events):
    import webview.worklog_api as mod
    monkeypatch.setattr(mod, "_recent_events", lambda limit: list(events))


def _owner():
    import webview.webgate as webgate
    return webgate.local_owner_id()


# --- the classified stream -------------------------------------------------- #

def test_stamps_each_event_with_its_class_and_names_the_class_set(monkeypatch):
    owner = _owner()
    events = [
        _event(ek.WALLET_SPEND, user_id=owner, ts=5.0),
        _event(ek.CRON_RUN, user_id=owner, ts=4.0),
        _event(ek.GOAL_RUN, user_id=owner, ts=3.0),
        _event(ek.OWNER_NOTICE, user_id=owner, ts=2.0),
        _event("tool_result", user_id=owner, ts=1.0),
    ]
    _stub_events(monkeypatch, events)
    client = _client(monkeypatch, posture="local")
    resp = client.get("/api/webgate/log")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unreadable"] is None
    # It names the class set it read into (the chips are built from this).
    assert body["classes"] == list(CLIENT_CLASSES)
    seen = {e["cls"] for e in body["entries"]}
    assert seen == {"money", "cron", "goal", "message", "tool"}
    for e in body["entries"]:
        assert e["cls"] == classify(e["kind"])
        assert e["cls"] in CLIENT_CLASSES
    # Newest first.
    ts = [e["ts"] for e in body["entries"]]
    assert ts == sorted(ts, reverse=True)


def test_a_diagnostic_kind_is_hidden_by_default_and_shown_on_request(monkeypatch):
    owner = _owner()
    assert is_diagnostic("step")  # the kind this test leans on is a diagnostic
    events = [
        _event(ek.GOAL_RUN, user_id=owner, ts=2.0),
        _event("step", user_id=owner, ts=1.0),
    ]
    _stub_events(monkeypatch, events)
    client = _client(monkeypatch, posture="local")

    default = client.get("/api/webgate/log").json()
    kinds = {e["kind"] for e in default["entries"]}
    assert "step" not in kinds, "a diagnostic kind rendered without being asked for"
    assert ek.GOAL_RUN in kinds

    with_diag = client.get("/api/webgate/log?diagnostics=1").json()
    diag = {e["kind"]: e for e in with_diag["entries"]}
    assert "step" in diag and diag["step"]["diagnostic"] is True


def test_raw_carries_the_exact_payload_and_the_default_does_not(monkeypatch):
    owner = _owner()
    events = [_event(ek.GOAL_RUN, user_id=owner, payload={"goal_id": "g7", "why": "test"})]
    _stub_events(monkeypatch, events)
    client = _client(monkeypatch, posture="local")

    lean = client.get("/api/webgate/log").json()["entries"][0]
    assert "payload" not in lean

    raw = client.get("/api/webgate/log?raw=1").json()["entries"][0]
    assert raw["payload"] == {"goal_id": "g7", "why": "test"}


def test_class_filters_to_one_class(monkeypatch):
    owner = _owner()
    events = [
        _event(ek.WALLET_SPEND, user_id=owner, ts=2.0),
        _event(ek.GOAL_RUN, user_id=owner, ts=1.0),
    ]
    _stub_events(monkeypatch, events)
    client = _client(monkeypatch, posture="local")
    body = client.get("/api/webgate/log?class=money").json()
    assert {e["cls"] for e in body["entries"]} == {"money"}


def test_an_unknown_class_is_ignored_not_a_400(monkeypatch):
    owner = _owner()
    _stub_events(monkeypatch, [_event(ek.GOAL_RUN, user_id=owner)])
    client = _client(monkeypatch, posture="local")
    resp = client.get("/api/webgate/log?class=not_a_class")
    assert resp.status_code == 200
    assert len(resp.json()["entries"]) == 1


# --- honest states ---------------------------------------------------------- #

def test_an_unreadable_source_is_named_never_a_silent_empty_list(monkeypatch):
    import webview.worklog_api as mod

    def _boom(limit):
        raise OSError("disk is on fire")

    monkeypatch.setattr(mod, "_recent_events", _boom)
    client = _client(monkeypatch, posture="local")
    resp = client.get("/api/webgate/log")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 043 A11: entries is NULL on a read fault — an empty list is the sentence
    # "a quiet day", which is what this endpoint must never say by accident.
    assert body["entries"] is None
    assert body["filtered_out"] is None
    assert body["unreadable"] and "disk is on fire" in body["unreadable"]


def test_a_genuine_empty_stream_is_not_unreadable(monkeypatch):
    _stub_events(monkeypatch, [])
    client = _client(monkeypatch, posture="local")
    body = client.get("/api/webgate/log").json()
    assert body["entries"] == []
    assert body["unreadable"] is None


# --- tenant isolation ------------------------------------------------------- #

def test_another_tenants_events_are_never_returned(monkeypatch):
    """Another tenant's row is dropped AND counted; an UNTENANTED row is kept.

    ⚠️ 043 A11: an event with an empty ``user_id`` belongs to this tenant on a
    single-owner instance — several writers record no tenant stamp, and the
    status snapshot's own filter is ``(user_id = ? OR user_id = '')``. Dropping
    them made Work › Log a strictly smaller day than every other surface.
    """
    owner = _owner()
    events = [
        _event(ek.GOAL_RUN, user_id=owner, ts=2.0),
        _event(ek.WALLET_SPEND, user_id="someone-else", ts=1.0),
        _event(ek.CRON_RUN, user_id="", ts=1.0),   # untenanted: this owner's
    ]
    _stub_events(monkeypatch, events)
    client = _client(monkeypatch, posture="local")
    body = client.get("/api/webgate/log").json()
    assert {e["kind"] for e in body["entries"]} == {ek.GOAL_RUN, ek.CRON_RUN}
    # The one row that was removed is COUNTED, so a short list is visibly short.
    assert body["filtered_out"] == 1


def test_unbound_own_ops_console_403s(monkeypatch):
    _stub_events(monkeypatch, [])
    client = _client(monkeypatch, posture="own_ops")
    resp = client.get("/api/webgate/log")
    assert resp.status_code == 403, resp.text


# --- the router carries the path -------------------------------------------- #

def test_router_exposes_the_log_path():
    import webview.worklog_api as mod
    routes = [r for r in mod.router.routes
              if getattr(r, "path", None) == "/api/webgate/log"]
    assert routes, "log route missing from the router"
    assert "GET" in routes[0].methods
