"""WS-3.1 (2026-07-07): the console message path delivers IN-PROCESS.

Prod runs a single service (polyrob-webview.service) with the TaskAgent in
the same process; there is no :9000 api service. POST
/api/session/{id}/messages used to hard-proxy to http://127.0.0.1:9000 and
always fail. It must now call the in-process task-router handler when the
task router is mounted and a TaskAgent is registered — and keep the proxy as
the fallback for the classic two-service shape.
"""
import importlib
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


def _local_client(monkeypatch):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return srv, TestClient(srv._fastapi)


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    yield
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    import webview.server as server
    importlib.reload(server)


def _install_fake_agent(monkeypatch, agent):
    import core.container as cc

    container = MagicMock()
    container.get_agent.return_value = agent
    container.get_service.return_value = agent
    monkeypatch.setattr(cc.DependencyContainer, "get_instance",
                        classmethod(lambda cls, *a, **k: container), raising=False)


def test_message_send_uses_in_process_handler(monkeypatch):
    srv, client = _local_client(monkeypatch)
    fake_agent = MagicMock(name="task_agent")
    _install_fake_agent(monkeypatch, fake_agent)

    calls = {}

    async def _fake_task_send(session_id, payload, req, agent):
        calls["session_id"] = session_id
        calls["text"] = payload.text
        calls["kind"] = payload.kind
        calls["attached_files"] = payload.attached_files
        calls["state_user"] = getattr(req.state, "user_id", None)
        calls["agent"] = agent
        return {"status": "queued"}

    import api.task_http_api as th
    monkeypatch.setattr(th, "send_user_message", _fake_task_send)

    resp = client.post("/api/session/sess-abc/messages", json={
        "text": "hello agent", "kind": "comment", "metadata": {},
        "attached_files": ["a.png"],
    })
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert calls["session_id"] == "sess-abc"
    assert calls["text"] == "hello agent"
    assert calls["attached_files"] == ["a.png"]
    assert calls["agent"] is fake_agent
    # local posture: identity aligned to the session owner (the local owner)
    assert calls["state_user"] == srv.webgate.local_owner_id()


def test_in_process_409_surfaces_honest_remote_answer(monkeypatch):
    """guard_remote's 409 (session live in ANOTHER process) must reach the
    client as an honest 409, not a false-404 or fake success."""
    from fastapi import HTTPException

    srv, client = _local_client(monkeypatch)
    _install_fake_agent(monkeypatch, MagicMock(name="task_agent"))

    async def _remote(session_id, payload, req, agent):
        raise HTTPException(status_code=409,
                            detail={"error": "session_owned_by_other_worker",
                                    "owner_pid": 4242},
                            headers={"Retry-After": "2"})

    import api.task_http_api as th
    monkeypatch.setattr(th, "send_user_message", _remote)

    resp = client.post("/api/session/sess-abc/messages", json={"text": "hi"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["success"] is False
    assert "agent process" in body["error"]
    assert body["detail"]["owner_pid"] == 4242


def test_in_process_404_maps_to_wrapper_shape(monkeypatch):
    from fastapi import HTTPException

    srv, client = _local_client(monkeypatch)
    _install_fake_agent(monkeypatch, MagicMock(name="task_agent"))

    async def _missing(session_id, payload, req, agent):
        raise HTTPException(status_code=404, detail="Session not found")

    import api.task_http_api as th
    monkeypatch.setattr(th, "send_user_message", _missing)

    resp = client.post("/api/session/sess-abc/messages", json={"text": "hi"})
    assert resp.status_code == 404
    assert resp.json()["success"] is False


def test_no_in_process_agent_falls_back_to_proxy(monkeypatch):
    """Classic two-service shape: no TaskAgent in this process → the :9000
    proxy path runs (stubbed httpx returns 200)."""
    srv, client = _local_client(monkeypatch)
    _install_fake_agent(monkeypatch, None)

    seen = {}

    class _FakeResp:
        status_code = 200
        text = "ok"

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            seen["url"] = url
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient())

    resp = client.post("/api/session/sess-abc/messages", json={"text": "hi"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert "127.0.0.1:9000" in seen["url"]


def test_queue_status_uses_in_process_handler(monkeypatch):
    srv, client = _local_client(monkeypatch)
    _install_fake_agent(monkeypatch, MagicMock(name="task_agent"))

    async def _fake_queue(session_id, req, agent):
        return {"queued_messages": 3, "agent_status": "running",
                "streaming_callbacks": 1, "callback_failures": 0}

    import api.task_http_api as th
    monkeypatch.setattr(th, "get_queue_status", _fake_queue)

    resp = client.get("/api/session/sess-abc/queue-status")
    assert resp.status_code == 200
    assert resp.json() == {"queued_messages": 3, "agent_status": "running",
                           "streaming_callbacks": 1, "callback_failures": 0}
