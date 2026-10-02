"""070 W0.14 — the workspace tree is capped, depth-limited, on demand.

The old route in ``webview/server.py`` walked the whole workspace with no limit
(on prod the shared project folder), sorted by name, and was re-fetched on every
tick. It now lives in ``webview/workspace_routes.py``.
"""
import importlib
import os
import time

import pytest
from fastapi.testclient import TestClient

PATH = "/api/session/{sid}/workspace/tree"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    import agents.task.path as pth
    pth.reset_path_manager()
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    yield TestClient(srv._fastapi), srv
    for key in ("POLYROB_POSTURE", "ENV"):
        monkeypatch.delenv(key, raising=False)
    importlib.reload(wg)
    importlib.reload(srv)


def _ws(sid):
    import agents.task.path as pth
    from core.identity import ANON_USER_ID
    clean = pth.pm().clean_session_id(sid)
    ws = pth.pm().get_workspace_dir(clean, user_id=ANON_USER_ID)
    ws.mkdir(parents=True, exist_ok=True)
    # the feed folder makes the session discoverable under its owner
    pth.pm().get_feed_dir(clean, user_id=ANON_USER_ID).mkdir(parents=True, exist_ok=True)
    return ws


def _get(http, sid, query=""):
    return http.get(PATH.format(sid=sid) + query)


def test_cap_500(client):
    http, _ = client
    ws = _ws("tree-cap")
    for i in range(1000):
        (ws / f"f{i:04d}.txt").write_text("x")
    body = _get(http, "tree-cap").json()
    assert body["total"] == 500
    assert len(body["children"]) == 500
    assert body["truncated"] is True


def test_depth_4(client):
    http, _ = client
    ws = _ws("tree-depth")
    deep = ws / "a" / "b" / "c" / "d" / "e" / "f"
    deep.mkdir(parents=True)
    (deep / "leaf.txt").write_text("x")
    body = _get(http, "tree-depth").json()
    level = body["children"][0]                          # a (level 1)
    for _ in range(3):                                   # b, c, d
        level = level["children"][0]
    assert level["name"] == "d"
    assert level["children"] is None
    assert level["truncated"] is True
    assert body["truncated"] is True


def test_skip_dirs(client):
    http, _ = client
    ws = _ws("tree-skip")
    for name in ("node_modules", "__pycache__", ".git", "src"):
        (ws / name).mkdir()
    names = {c["name"] for c in _get(http, "tree-skip").json()["children"]}
    assert names == {"src"}


def test_newest_first(client):
    http, _ = client
    ws = _ws("tree-new")
    now = time.time()
    for i, name in enumerate(("old.txt", "mid.txt", "new.txt")):
        path = ws / name
        path.write_text("x")
        os.utime(path, (now - 100 + i * 10, now - 100 + i * 10))
    names = [c["name"] for c in _get(http, "tree-new").json()["children"]]
    assert names == ["new.txt", "mid.txt", "old.txt"]


def test_path_subfolder(client):
    http, _ = client
    ws = _ws("tree-sub")
    (ws / "inbound").mkdir()
    (ws / "inbound" / "given.pdf").write_text("x")
    (ws / "other.txt").write_text("x")
    body = _get(http, "tree-sub", "?path=inbound&depth=1").json()
    assert [c["name"] for c in body["children"]] == ["given.pdf"]
    missing = _get(http, "tree-sub", "?path=nope").json()
    assert missing["children"] == [] and missing["total"] == 0


def test_path_escape_is_403(client):
    http, _ = client
    _ws("tree-esc")
    for bad in ("../x", "/etc", "a/../../x", "..\\x"):
        assert _get(http, "tree-esc", f"?path={bad}").status_code == 403, bad


def test_shared_flag_follows_project_root_mode(client, monkeypatch):
    http, _ = client
    _ws("tree-shared")
    assert _get(http, "tree-shared").json()["shared"] is False
    import agents.task.path as pth
    monkeypatch.setattr(type(pth.pm()), "is_project_root_workspace", property(lambda self: True))
    assert _get(http, "tree-shared").json()["shared"] is True


def test_error_hides_the_exception(client, monkeypatch):
    http, _ = client
    _ws("tree-err")
    import webview.workspace_routes as wr

    def boom(*a, **k):
        raise RuntimeError("secret /var/lib/polyrob/path")
    monkeypatch.setattr(wr, "walk_tree", boom)
    resp = _get(http, "tree-err")
    assert resp.status_code == 500
    assert resp.json() == {"name": "workspace", "type": "dir", "children": None,
                           "error": "unreadable"}
    assert "secret" not in resp.text


def _routes(routes, depth=0):
    for route in routes or ():
        yield route
        inner = getattr(route, "original_router", None) or getattr(route, "app", None)
        if depth < 6 and hasattr(inner, "routes"):
            yield from _routes(inner.routes, depth + 1)


def test_route_is_served_once(client):
    _http, srv = client
    hits = [r for r in _routes(srv._fastapi.routes)
            if getattr(r, "path", None) == "/api/session/{session_id}/workspace/tree"]
    assert len(hits) == 1, hits
    assert hits[0].endpoint.__module__ == "webview.workspace_routes"
