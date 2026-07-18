"""T10 (corrections item 6): webview `/api/webgate/pending` parity with
`polyrob owner pending` — the aggregator must ALSO surface queued tool-approval
asks and pending correspondent bindings (not just self-evolution proposals),
and promote/reject must route them through the SAME underlying functions the
CLI verbs call (``decide_tool_approval`` / ``CorrespondentRegistry.approve``) —
never a reimplementation of the grant logic.
"""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, user_id="gleb"):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


def _seed_tool_approval_ask(tmp_path, user_id="gleb"):
    from agents.task.goals.board import GoalBoard
    board = GoalBoard(os.path.join(str(tmp_path), "goals.db"))
    ask = board.create_ask(
        user_id=user_id, what="Approve x402_request? [abc123]",
        why="tool=x402_request", extra_payload={
            "ask_kind": "tool_approval", "tool_name": "x402_request",
            "request_hash": "abc123", "grant_consumed": False,
        }, force=True,
    )
    return ask.id


def _seed_pending_correspondent(tmp_path, user_id="gleb", surface="email",
                                address="third.party@example.com"):
    from core.surfaces.correspondents import CorrespondentRegistry
    registry = CorrespondentRegistry(os.path.join(str(tmp_path), "correspondents.db"))
    registry.seed(surface=surface, address=address, session_id="sess_1",
                 user_id=user_id, require_approval=True)
    return registry


# --- aggregation --------------------------------------------------------------

def test_pending_includes_queued_tool_approval(monkeypatch, tmp_path):
    from tools.controller.approval_queue import tap_display_id
    ask_id = _seed_tool_approval_ask(tmp_path)
    client, _ = _client(monkeypatch, tmp_path)
    r = client.get("/api/webgate/pending")
    assert r.status_code == 200
    items = r.json()["items"]
    tap_items = [it for it in items if it["kind"] == "tool_approval"]
    assert [it["id"] for it in tap_items] == [tap_display_id(ask_id)]


def test_pending_includes_pending_correspondent(monkeypatch, tmp_path):
    _seed_pending_correspondent(tmp_path)
    client, _ = _client(monkeypatch, tmp_path)
    r = client.get("/api/webgate/pending")
    assert r.status_code == 200
    items = r.json()["items"]
    corr_items = [it for it in items if it["kind"] == "correspondent"]
    assert [it["id"] for it in corr_items] == ["email:third.party@example.com"]


# --- promote / reject route through the same underlying functions -----------

def test_promote_tool_approval_approves_via_decide_tool_approval(monkeypatch, tmp_path):
    from agents.task.goals.board import GoalBoard
    from tools.controller.approval_queue import tap_display_id
    ask_id = _seed_tool_approval_ask(tmp_path)
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post(f"/api/webgate/pending/tool_approval/{tap_display_id(ask_id)}/promote")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    board = GoalBoard(os.path.join(str(tmp_path), "goals.db"))
    row = board.get(ask_id)
    assert (row.payload or {}).get("decision") == "approved"


def test_reject_tool_approval_declines(monkeypatch, tmp_path):
    from agents.task.goals.board import GoalBoard
    from tools.controller.approval_queue import tap_display_id
    ask_id = _seed_tool_approval_ask(tmp_path)
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post(f"/api/webgate/pending/tool_approval/{tap_display_id(ask_id)}/reject")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    board = GoalBoard(os.path.join(str(tmp_path), "goals.db"))
    row = board.get(ask_id)
    assert (row.payload or {}).get("decision") == "rejected"


def test_promote_correspondent_approves_via_registry(monkeypatch, tmp_path):
    registry = _seed_pending_correspondent(tmp_path)
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/api/webgate/pending/correspondent/email:third.party@example.com/promote")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    rows = registry.list(user_id="gleb")
    assert rows[0]["state"] == "active"


def test_reject_correspondent_is_honest_not_a_500(monkeypatch, tmp_path):
    """No CLI/registry reject primitive exists for a pending correspondent
    (only approve()) — this must degrade to an honest ok:false, never a 500 or
    a reimplemented grant/deny mechanism."""
    _seed_pending_correspondent(tmp_path)
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/api/webgate/pending/correspondent/email:third.party@example.com/reject")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_promote_unknown_correspondent_is_ok_false(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/api/webgate/pending/correspondent/email:nobody@example.com/promote")
    assert r.status_code == 200
    assert r.json()["ok"] is False


# --- read-only gate still applies to the new kinds ---------------------------

def test_read_only_blocks_promote_for_new_kinds(monkeypatch, tmp_path):
    ask_id = _seed_tool_approval_ask(tmp_path)
    from tools.controller.approval_queue import tap_display_id
    _seed_pending_correspondent(tmp_path)
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    assert client.get("/api/webgate/pending").status_code == 200
    assert client.post(
        f"/api/webgate/pending/tool_approval/{tap_display_id(ask_id)}/promote"
    ).status_code == 403
    assert client.post(
        "/api/webgate/pending/correspondent/email:third.party@example.com/promote"
    ).status_code == 403
