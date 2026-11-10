"""113 W2: the console's chats — one main chat that `Chat` opens, separate chats
beside it, and no row of another tenant (webview/chats_routes.py)."""
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.surfaces.session_chat_registry import SessionChatRegistry
from tests.unit.webview.owner_session import authenticate_render_app


@pytest.fixture()
def routes(monkeypatch, tmp_path):
    import webview.chat_open as chat_open
    import webview.chats_routes as mod
    registry = SessionChatRegistry(str(tmp_path / "surfaces.db"))
    who = {"id": "u_a"}
    monkeypatch.setattr(mod, "_registry", lambda: registry)
    monkeypatch.setattr(mod, "_tenant", lambda request: who["id"])
    monkeypatch.setattr(chat_open, "chat_exists", lambda sid: sid != "gone0000")
    monkeypatch.setattr(chat_open, "valid_session_id", lambda sid: bool(sid))
    return types.SimpleNamespace(mod=mod, registry=registry, who=who)


def _created(routes, sid):
    routes.mod.bind_created(None, types.SimpleNamespace(session_id=sid))


def test_the_first_console_chat_is_main_and_the_next_ones_are_separate(routes):
    _created(routes, "s1")
    _created(routes, "s2")
    base = routes.mod.base_key("u_a")
    assert routes.registry.resolve(base)["session_id"] == "s1"
    assert routes.registry.resolve(base + ":chat:c2")["session_id"] == "s2"
    rows = routes.mod.annotate(None, [{"id": "s1"}, {"id": "s2"}, {"id": "other"}])
    assert {k: rows[0]["chat"][k] for k in ("slug", "label", "main")} == {
        "slug": "main", "label": "", "main": True}
    assert "active_iso" in rows[0]["chat"]          # 113 W6: the chat column's order
    assert rows[1]["chat"]["slug"] == "c2" and "chat" not in rows[2]


def test_chat_opens_the_main_chat_and_a_lost_main_chat_is_replaced(routes):
    assert routes.mod.main_redirect(None) is None           # nothing yet: the cold open
    _created(routes, "gone0000")
    assert routes.mod.main_redirect(None) is None           # its folder is gone
    _created(routes, "s2")                                  # so the next chat is the main one
    go = routes.mod.main_redirect(None)
    assert go.status_code == 303 and go.headers["location"] == "/c/s2"
    assert len(routes.registry.chat_rows(user_id="u_a")) == 1


def test_a_chat_store_fault_changes_nothing(routes, monkeypatch):
    monkeypatch.setattr(routes.mod, "_registry", lambda: None)
    _created(routes, "s1")
    assert routes.mod.main_redirect(None) is None
    assert routes.mod.annotate(None, [{"id": "s1"}]) == [{"id": "s1"}]


def test_another_tenant_sees_and_changes_nothing(routes):
    _created(routes, "s1")
    _created(routes, "s2")
    routes.who["id"] = "u_b"
    assert routes.mod.main_redirect(None) is None
    assert routes.mod.annotate(None, [{"id": "s1"}, {"id": "s2"}]) == [{"id": "s1"}, {"id": "s2"}]
    app = FastAPI()
    from webview.pages_new import api_router
    app.include_router(api_router)
    authenticate_render_app(app)
    client = TestClient(app)
    assert client.post("/api/webgate/chats/c2/close").status_code == 404
    assert client.post("/api/webgate/chats/c2/rename", json={"label": "mine"}).status_code == 404
    row = routes.registry.chat_row(routes.mod.base_key("u_a") + ":chat:c2")
    assert row["closed_at"] is None and not row["label"]


def test_rename_and_close_act_on_the_named_chat_and_main_stays_open(routes, monkeypatch):
    import webview.webgate as webgate
    closed = []
    monkeypatch.setattr(webgate, "in_process_task_agent", lambda: types.SimpleNamespace(
        close_chat_episode=closed.append))
    _created(routes, "s1")
    _created(routes, "s2")
    app = FastAPI()
    from webview.pages_new import api_router
    app.include_router(api_router)
    authenticate_render_app(app)
    client = TestClient(app)
    base = routes.mod.base_key("u_a")
    assert client.post("/api/webgate/chats/c2/rename", json={"label": "tax letter"}).json()["success"]
    assert routes.registry.chat_row(base + ":chat:c2")["label"] == "tax letter"
    assert routes.registry.chat_row(base)["label"] in (None, "")
    assert client.post("/api/webgate/chats/main/close").status_code == 400      # main stays
    assert client.post("/api/webgate/chats/c2/close").json()["success"]
    assert closed == [base + ":chat:c2"]
    assert routes.registry.chat_row(base + ":chat:c2")["closed_at"] is not None
    assert "chat" not in routes.mod.annotate(None, [{"id": "s2"}])[0]           # closed: not a chat row


def test_the_chat_destination_redirects_and_new_chat_does_not(routes, monkeypatch):
    import webview.chat_open as chat_open
    import webview.pages_new as pages_new
    from core.surfaces.inbox import compose
    monkeypatch.setattr(pages_new, "_pause_headline", lambda: "")
    monkeypatch.setattr(pages_new, "_read_only", lambda: False)
    monkeypatch.setattr(pages_new, "_inbox_summary", lambda request: compose([], {"asks": "ok"}))
    monkeypatch.setattr(chat_open, "_tenant", lambda request: ("u_a", None))
    monkeypatch.setattr(chat_open, "_recap", lambda user_id: [])
    app = FastAPI()
    app.include_router(pages_new.router)
    authenticate_render_app(app)
    client = TestClient(app, follow_redirects=False)
    assert client.get("/").status_code == 200                 # no main chat: the cold open
    _created(routes, "s1")
    go = client.get("/")
    assert go.status_code == 303 and go.headers["location"] == "/c/s1"
    page = client.get("/?new=1")
    assert page.status_code == 200 and 'href="/?new=1"' in page.text   # the New chat link


def test_the_chat_box_runs_the_five_verbs_for_the_chat_of_its_page(routes, monkeypatch):
    monkeypatch.setattr(routes.mod.webgate, "in_process_task_agent", lambda: None)
    monkeypatch.setattr(routes.mod, "console_write", lambda *a, **k: None)
    _created(routes, "s1")
    _created(routes, "s2")
    box = routes.mod.box_verb
    listing = box("u_a", "s2", "/chats", [])
    assert "1. main" in listing and "/c/s1" in listing
    assert "you are here  /c/s2" in listing and "next start" not in listing
    assert "named: notes" in box("u_a", "s2", "/rename", ["notes"])     # this page's chat
    base = routes.mod.base_key("u_a")
    assert routes.registry.chat_row(base + ":chat:c2")["label"] == "notes"
    assert not routes.registry.chat_row(base)["label"]
    assert box("u_a", "s2", "/chat", ["main"]).endswith("/c/s1")        # the page, no move
    assert "stays open" in box("u_a", "s1", "/close", [])               # main never closes
    closed = box("u_a", "s2", "/close", [])
    assert "Closed: notes" in closed and closed.endswith("/c/s1")
    assert routes.registry.chat_row(base + ":chat:c2")["closed_at"] is not None


def test_new_in_the_chat_box_names_the_next_chat_the_console_starts(routes, monkeypatch):
    monkeypatch.setattr(routes.mod, "console_write", lambda *a, **k: None)
    _created(routes, "s1")
    text = routes.mod.box_verb("u_a", "s1", "/new", ["plans"])
    assert "New chat: plans." in text and "/?new=1" in text
    _created(routes, "s2")                                   # the page New chat opened
    rows = routes.registry.chat_rows(user_id="u_a")
    assert len(rows) == 2                                    # that row, not a third one
    assert routes.registry.resolve_by_session_id("s2")["label"] == "plans"


def test_the_chat_box_never_reads_another_tenant_or_a_page_with_no_row(routes, monkeypatch):
    monkeypatch.setattr(routes.mod, "console_write", lambda *a, **k: None)
    _created(routes, "s1")
    _created(routes, "s2")
    box = routes.mod.box_verb
    assert "/c/s" not in box("u_b", "s2", "/chats", [])
    assert "not in your chat list" in box("u_b", "s2", "/rename", ["mine"])
    assert "not in your chat list" in box("u_b", "s2", "/close", [])
    assert "No chat" in box("u_b", "s2", "/close", ["c2"])
    row = routes.registry.chat_row(routes.mod.base_key("u_a") + ":chat:c2")
    assert row["closed_at"] is None and not row["label"]
    monkeypatch.setattr(routes.mod, "_registry", lambda: None)
    assert "could not load" in box("u_a", "s1", "/chats", [])


def test_a_waiting_chat_is_replaced_by_the_next_new_and_expires(routes, monkeypatch):
    """Review 2026-11-10: `/new a` then `/new b` left `a` as a row for ever, and an old
    waiting row gave its name to an unrelated chat a week later."""
    monkeypatch.setattr(routes.mod, "console_write", lambda *a, **k: None)
    _created(routes, "s1")
    routes.mod.box_verb("u_a", "s1", "/new", ["first"])
    routes.mod.box_verb("u_a", "s1", "/new", ["second"])
    open_rows = [r for r in routes.registry.chat_rows(user_id="u_a") if r["closed_at"] is None]
    assert sorted(r["label"] or "" for r in open_rows) == ["", "second"]
    monkeypatch.setattr(routes.mod, "WAITING_SECS", -1)        # it waited too long
    _created(routes, "s2")
    assert routes.registry.resolve_by_session_id("s2")["label"] == ""
