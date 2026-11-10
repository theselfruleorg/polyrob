"""113 W5 item 2 (ST-1, CH-1, ST-2) — an empty store is not an unreadable one.

A fresh install showed "I could not read…" on the chat timeline (a chat whose
feed was not written yet answered 404) and on Agent › Abilities (a local
console with no operator MCP file named that as an error). A chat id that does
not exist reads "This chat does not exist" with a New chat link. One empty-state
partial (``templates/_state.html``) draws the server-rendered empty state.
"""
import importlib
import json

import pytest
from bs4 import BeautifulSoup
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
    yield TestClient(srv._fastapi, headers=owner_headers(monkeypatch))
    for key in ("POLYROB_POSTURE", "ENV"):
        monkeypatch.delenv(key, raising=False)
    importlib.reload(wg)
    importlib.reload(srv)


# --- the chat timeline ------------------------------------------------------ #

def test_a_chat_with_no_feed_yet_is_empty_not_unreadable(client):
    import agents.task.path as pth
    from core.identity import ANON_USER_ID
    clean = pth.pm().clean_session_id("sess-fresh")
    root = pth.pm().get_session_root(clean, user_id=ANON_USER_ID)
    assert root.is_dir() and not (root / "feed").exists()
    resp = client.get(PATH.format(sid="sess-fresh"))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"events": [], "last_seq": 0, "total": 0, "has_more": False, "read": 0}
    assert not (root / "feed").exists()          # a read never creates the feed


def test_a_chat_that_does_not_exist_is_still_404(client):
    resp = client.get(PATH.format(sid="sess-never-made"))
    assert resp.status_code == 404


# --- a chat id that does not exist ------------------------------------------ #

def test_a_missing_chat_says_so_and_offers_a_new_chat(client):
    resp = client.get("/c/a b")
    assert resp.status_code == 404
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.select_one(".error-message").get_text(strip=True) == "This chat does not exist."
    link = soup.select_one('a.action-link[href="/?new=1"]')
    assert link is not None and link.get_text(strip=True) == "New chat"


def test_a_missing_page_keeps_its_own_lead_and_no_new_chat(client):
    soup = BeautifulSoup(client.get("/no-such-page").text, "html.parser")
    assert soup.select_one(".error-message").get_text(strip=True) == "This page does not exist."
    assert soup.select_one('a[href="/?new=1"]') is None


def test_read_only_offers_no_new_chat(monkeypatch):
    from types import SimpleNamespace
    from webview import error_page
    monkeypatch.setattr(error_page, "_read_only", lambda: True)
    req = SimpleNamespace(url=SimpleNamespace(path="/c/x"))
    ctx = error_page.error_context(req, 404, "Not Found")
    assert ctx["new_chat"] is False
    from webview.copy import t
    assert ctx["lead"] == t("error.chat_not_found")


# --- Agent › Abilities: the operator MCP file ------------------------------- #

NOTE = "no operator file at /x/mcp.json in the console process"


@pytest.fixture()
def mcp_view(monkeypatch, tmp_path):
    from webview import mcp_view as mod
    import tools.mcp.inventory as inv
    import tools.mcp.operator_home as home
    path = tmp_path / "mcp.json"
    monkeypatch.setattr(home, "operator_mcp_file", lambda: path)
    monkeypatch.setattr(inv, "operator_servers", lambda container: ({}, NOTE))
    return mod, path


def test_local_console_with_no_operator_file_is_empty(monkeypatch, mcp_view):
    mod, _path = mcp_view
    monkeypatch.setattr("webview.webgate.posture", lambda: "local")
    out = mod.local_absent_file_is_empty({"items": [], "error": NOTE}, None)
    assert out["error"] is None and out["items"] == []


def test_the_tenant_leg_error_survives(monkeypatch, mcp_view):
    mod, _path = mcp_view
    monkeypatch.setattr("webview.webgate.posture", lambda: "local")
    out = mod.local_absent_file_is_empty(
        {"items": [], "error": "your own servers were not read; " + NOTE}, None)
    assert out["error"] == "your own servers were not read"


def test_a_server_console_keeps_the_note(monkeypatch, mcp_view):
    mod, _path = mcp_view
    monkeypatch.setattr("webview.webgate.posture", lambda: "own_ops")
    section = {"items": [], "error": NOTE}
    assert mod.local_absent_file_is_empty(section, None) is section


def test_an_operator_file_that_exists_keeps_its_error(monkeypatch, mcp_view):
    mod, path = mcp_view
    path.write_text("{not json")
    monkeypatch.setattr("webview.webgate.posture", lambda: "local")
    section = {"items": [], "error": "the operator file is not readable"}
    assert mod.local_absent_file_is_empty(section, None) is section


# --- the one empty-state partial -------------------------------------------- #

def test_the_state_partial_draws_one_shape():
    from jinja2 import Environment, FileSystemLoader
    import webview
    from pathlib import Path
    env = Environment(loader=FileSystemLoader(str(Path(webview.__file__).parent / "templates")),
                      autoescape=True)
    html = env.from_string(
        '{% from "_state.html" import state %}'
        '{{ state(title="T", body=["a", "b"], glyph="G", '
        'actions=[{"href": "/w", "label": "Go"}]) }}').render()
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("div.state")
    assert node.select_one(".state-glyph").get("aria-hidden") == "true"
    assert node.select_one("h2.state-title").get_text() == "T"
    assert [p.get_text() for p in node.select("p.state-body")] == ["a", "b"]
    assert node.select_one('.state-actions a.btn[href="/w"]').get_text() == "Go"
    bare = BeautifulSoup(env.from_string(
        '{% from "_state.html" import state %}{{ state(body=["only"]) }}').render(),
        "html.parser")
    assert bare.select_one("h2") is None and bare.select_one(".state-actions") is None


def test_the_inbox_uses_the_partial():
    import webview
    from pathlib import Path
    text = (Path(webview.__file__).parent / "templates" / "inbox.html").read_text()
    assert '{% from "_state.html" import state %}' in text
    assert 'class="state-glyph"' not in text
