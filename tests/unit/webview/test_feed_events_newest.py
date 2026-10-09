"""070 W0.10 — the feed backfill returns the NEWEST events, over both schemes.

The old route kept the first ``limit`` files by NAME: the oldest, and since a
telemetry file starts with digits and an ``add_to_feed`` file with a letter, a
long chat reopened on its first turn. It also matched ``?event_type=`` against
``{type}_*.json`` only, so a telemetry type (``000031_tool_started.json``) was
never found and ``agent_message`` was refused outright.
"""
import importlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient
from tests.unit.webview.owner_session import owner_headers

PATH = "/api/session/{sid}/feed/events"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    import agents.task.path as pth
    pth.reset_path_manager()
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    yield TestClient(srv._fastapi, headers=owner_headers(monkeypatch)), srv
    for key in ("POLYROB_POSTURE", "ENV"):
        monkeypatch.delenv(key, raising=False)
    importlib.reload(wg)
    importlib.reload(srv)


def _feed(sid):
    import agents.task.path as pth
    from core.identity import ANON_USER_ID
    clean = pth.pm().clean_session_id(sid)
    feed = pth.pm().get_feed_dir(clean, user_id=ANON_USER_ID)
    feed.mkdir(parents=True, exist_ok=True)
    return feed


def _write(feed, name, body, mtime):
    path = feed / name
    path.write_text(json.dumps(body))
    os.utime(path, (mtime, mtime))


def test_limit_keeps_the_newest_events(client):
    http, _srv = client
    feed = _feed("sess-newest")
    base = time.time() - 10_000
    for i in range(400):
        # Mix the schemes: even = telemetry (digit prefix), odd = add_to_feed.
        name = f"{i:06d}_tool_result.json" if i % 2 == 0 else f"agent_message_{int((base + i) * 1000)}.json"
        body = {"type": "tool_result" if i % 2 == 0 else "agent_message",
                "timestamp": base + i, "data": {"n": i}}
        if i % 2 == 0:
            body["_seq"] = i
        _write(feed, name, body, base + i)
    resp = http.get(PATH.format(sid="sess-newest") + "?limit=300")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ns = [e["data"]["n"] for e in body["events"]]
    assert ns == list(range(100, 400)), ns[:5]      # the newest 300, oldest first
    assert body["has_more"] is True
    assert body["total"] == 300


def test_event_type_matches_both_schemes(client):
    http, _srv = client
    feed = _feed("sess-schemes")
    now = time.time()
    _write(feed, "000031_tool_started.json",
           {"type": "tool_started", "timestamp": now, "_seq": 31, "data": {}}, now)
    _write(feed, "000032_tool_result.json",
           {"type": "tool_result", "timestamp": now + 1, "_seq": 32, "data": {}}, now + 1)
    _write(feed, f"agent_message_{int(now * 1000)}.json",
           {"type": "agent_message", "timestamp": now, "data": {"text": "hi"}}, now)
    started = http.get(PATH.format(sid="sess-schemes") + "?event_type=tool_started").json()
    assert [e["_source_file"] for e in started["events"]] == ["000031_tool_started.json"]
    reply = http.get(PATH.format(sid="sess-schemes") + "?event_type=agent_message")
    assert reply.status_code == 200, reply.text
    assert [e["type"] for e in reply.json()["events"]] == ["agent_message"]


def test_after_seq_still_filters(client):
    http, _srv = client
    feed = _feed("sess-after")
    now = time.time()
    for i in range(1, 6):
        _write(feed, f"{i:06d}_status.json",
               {"type": "status", "timestamp": now + i, "_seq": i, "data": {}}, now + i)
    body = http.get(PATH.format(sid="sess-after") + "?after_seq=3").json()
    assert [e["_seq"] for e in body["events"]] == [4, 5]
    assert body["last_seq"] == 5
    assert body["has_more"] is False


def test_an_unknown_type_and_a_bad_limit_are_refused(client):
    http, _srv = client
    _feed("sess-bad")
    assert http.get(PATH.format(sid="sess-bad") + "?event_type=bogus").status_code == 400
    assert http.get(PATH.format(sid="sess-bad") + "?limit=0").status_code == 400


def _routes(routes, depth=0):
    for route in routes or ():
        yield route
        inner = getattr(route, "original_router", None) or getattr(route, "app", None)
        if depth < 6 and hasattr(inner, "routes"):
            yield from _routes(inner.routes, depth + 1)


def test_route_is_served_once(client):
    _http, srv = client
    hits = [r for r in _routes(srv._fastapi.routes)
            if getattr(r, "path", None) == "/api/session/{session_id}/feed/events"]
    assert len(hits) == 1, hits
    assert hits[0].endpoint.__module__ == "webview.feed_routes"
