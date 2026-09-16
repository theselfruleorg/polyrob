"""A16/B1/B2 — GET /api/webgate/goals reads list_recent/status_counts/asks,
never GoalBoard.list() (the dispatcher's priority-ordered claim queue,
forbidden as a "what is on my board" view per AGENTS.md), and reports a
failed read honestly via ``error`` — mirroring ``api_cron`` exactly, the same
090 D4 shape as ``tests/unit/webview/test_invoices_settle.py::_client``.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, user_id="u1"):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pages.AutonomyConfig, "goals_enabled", staticmethod(lambda: True))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


class _ListForbidden:
    """A fake GoalBoard whose ``list()`` raises — proves the endpoint never
    calls it."""

    def __init__(self, *a, **k):
        pass

    def list(self, *a, **k):
        raise AssertionError("GoalBoard.list() must never back a view (B1/B2)")


class _FakeBoard(_ListForbidden):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = {}

    def list_recent(self, *, user_id=None, statuses=None, limit=30):
        self.calls["list_recent"] = {"user_id": user_id, "statuses": statuses, "limit": limit}
        return [_goal("g1", user_id, "ship it", "ready")]

    def status_counts(self, *, user_id=None):
        self.calls["status_counts"] = {"user_id": user_id}
        return {"ready": 1, "done": 3}

    def asks(self, *, user_id=None, status=None):
        self.calls["asks"] = {"user_id": user_id, "status": status}
        return [_goal("a1", user_id, "approve the spend?", "open")]


class _RaisingBoard(_ListForbidden):
    def list_recent(self, *, user_id=None, statuses=None, limit=30):
        raise RuntimeError("goals.db locked")

    def status_counts(self, *, user_id=None):
        raise RuntimeError("goals.db locked")

    def asks(self, *, user_id=None, status=None):
        raise RuntimeError("goals.db locked")


def _goal(id_, user_id, title, status):
    from agents.task.goals.board import Goal
    return Goal(id=id_, user_id=user_id or "u1", title=title, status=status)


def test_goals_reads_list_recent_status_counts_asks(monkeypatch, tmp_path):
    client, pages = _client(monkeypatch, tmp_path, user_id="u1")
    fake = _FakeBoard()
    monkeypatch.setattr(pages, "GoalBoard", lambda *a, **k: fake)

    r = client.get("/api/webgate/goals")
    assert r.status_code == 200
    body = r.json()

    assert body["enabled"] is True
    assert body["goals"][0]["id"] == "g1"
    assert body["counts"] == {"ready": 1, "done": 3}
    assert body["asks"][0]["id"] == "a1"
    assert "error" not in body

    assert fake.calls["list_recent"]["user_id"] == "u1"
    assert fake.calls["status_counts"]["user_id"] == "u1"
    assert fake.calls["asks"] == {"user_id": "u1", "status": "open"}


def test_goals_never_calls_list(monkeypatch, tmp_path):
    """The fake's list() raises AssertionError — a call would fail the test."""
    client, pages = _client(monkeypatch, tmp_path, user_id="u1")
    fake = _FakeBoard()
    monkeypatch.setattr(pages, "GoalBoard", lambda *a, **k: fake)

    r = client.get("/api/webgate/goals")
    assert r.status_code == 200  # would have 500'd if list() were called


def test_goals_read_error_is_reported_honestly(monkeypatch, tmp_path):
    """A raising board yields enabled:True, goals/counts/asks empty PLUS a
    named ``error`` — never a confidently-empty board."""
    client, pages = _client(monkeypatch, tmp_path, user_id="u1")
    monkeypatch.setattr(pages, "GoalBoard", lambda *a, **k: _RaisingBoard())

    r = client.get("/api/webgate/goals")
    assert r.status_code == 200
    body = r.json()

    assert body["enabled"] is True
    assert body["goals"] == []
    assert body["counts"] == {}
    assert body["asks"] == []
    assert "goals.db locked" in body["error"]


def test_goals_disabled_flag_unaffected(monkeypatch, tmp_path):
    client, pages = _client(monkeypatch, tmp_path, user_id="u1")
    monkeypatch.setattr(pages.AutonomyConfig, "goals_enabled", staticmethod(lambda: False))
    monkeypatch.setattr(pages, "GoalBoard", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("GoalBoard touched while GOALS disabled")))

    r = client.get("/api/webgate/goals")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "goals": []}


# NOTE: test_autonomy_js_renders_goals_error was deleted with the legacy
# autonomy page (043 §9 phase 3). The goals reader's ``error`` field is now
# rendered by the new shell's Work › Now (work-now.js), covered by
# webview/dev/work-now.test.js.
