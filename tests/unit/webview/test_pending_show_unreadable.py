"""`/api/webgate/pending/{kind}/{id}`: an item that is not found while a pending
store could not be read is "I could not read <store>", never "no pending …"."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, unavailable):
    import webview.pages as pages
    import tools.controller.approval_queue as aq
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "alice")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pages.webgate, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(aq, "all_pending", lambda **kw: aq.PendingSet([], list(unavailable)))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


def test_an_unreadable_store_is_named_not_reported_as_absent(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, ["queued tool + spend approvals"])
    body = client.get("/api/webgate/pending/tool_approval/tap_x").json()
    assert body["ok"] is False
    assert "no pending" not in body["body"]
    assert "queued tool + spend approvals" in body["body"]
    assert body["unavailable"] == ["queued tool + spend approvals"]


def test_all_stores_read_and_no_item_is_still_no_pending(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, [])
    body = client.get("/api/webgate/pending/tool_approval/tap_x").json()
    assert body == {"ok": False, "body": "no pending tool_approval 'tap_x'"}
