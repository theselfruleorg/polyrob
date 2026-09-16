"""043 W2 — `/api/repair/{session_id}` is a POST.

It rewrites a session's feed and llm_usage files (dedup, token re-estimation,
validation). As a GET it was a mutation any link, prefetch or crawler could
trigger, and it sat outside every rule this batch establishes: the read-only
guard could not see it (the guard passes GETs — a read-only console is still a
console), and the CSRF check could not either.

As a POST it carries `webgate.MUTATION_DEPS` like every other mutation, and the
in-body read_only() copy is gone.
"""
import importlib
import pathlib

import pytest
from fastapi.testclient import TestClient

_REPO = pathlib.Path(__file__).resolve().parents[3]


def _client(monkeypatch, posture="local", read_only=False, env="development"):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    monkeypatch.setenv("ENV", env)
    if read_only:
        monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    else:
        monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    for key in ("POLYROB_POSTURE", "WEBVIEW_READ_ONLY", "ENV"):
        monkeypatch.delenv(key, raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)


def test_the_get_is_gone(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/api/repair/sess-x").status_code == 405


def test_the_post_exists(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/repair/sess-x")
    assert resp.status_code != 405
    assert resp.status_code != 404 or "Session not found" in resp.text


def test_read_only_refuses_the_post(monkeypatch):
    client = _client(monkeypatch, read_only=True)
    resp = client.post("/api/repair/sess-x")
    assert resp.status_code == 403
    assert "read-only" in resp.text.lower()


def test_a_cross_site_post_is_refused(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/repair/sess-x",
                       headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_the_page_script_posts(monkeypatch):
    """The only caller is the Repair button on error.html — it must not still
    be issuing a GET the route no longer answers.

    043 phase 5 (R6): error.html's behaviour was de-inlined into
    ``static/js/error-page.js`` (the console CSP dropped ``'unsafe-inline'``),
    so the assertion moved with it. error.html still LOADS that module.
    """
    html = (_REPO / "webview" / "templates" / "error.html").read_text(encoding="utf-8")
    assert "/static/js/error-page.js" in html
    js = (_REPO / "webview" / "static" / "js" / "error-page.js").read_text(encoding="utf-8")
    assert "/api/repair/" in js
    fetch_call = js.split("/api/repair/")[1]
    assert "method: 'POST'" in fetch_call[:400] or 'method: "POST"' in fetch_call[:400]
