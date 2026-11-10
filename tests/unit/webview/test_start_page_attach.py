"""113 W6 item 2 (C-02): a start-page attach creates the chat unstarted
(``wait_for_uploads``), uploads, then starts it with the first line. The create
route lets an unstarted create through with no model (an API client may start it
elsewhere), so the console's create hop gives the same 503 ``no_model`` the
start page and the message route give, before a chat folder exists. A line to an
idle chat keeps its refusal, now with the machine code at the top level.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Container:
    def __init__(self, llm):
        self._llm = llm

    def get_service(self, name):
        return self._llm if name == "llm" else None


def _client(monkeypatch, llm):
    import webview.console_commands as cc
    import webview.pages as pages
    import api.task_http_api as thp
    from fastapi.responses import JSONResponse
    created = []

    async def create(request_body, req, agent=None):
        created.append(request_body)
        return JSONResponse({"success": True, "session_id": "sess-1"})

    monkeypatch.setattr(thp, "create_session", create)
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "alice")
    app = FastAPI()
    app.include_router(cc.build_console_create_router(), prefix="/api")
    app.dependency_overrides[thp.get_task_agent] = lambda: SimpleNamespace(
        container=_Container(llm))
    return TestClient(app), created


@pytest.mark.parametrize("body", [
    {"task": "read this", "auto_start": False, "wait_for_uploads": True},
    {"task": "read this", "wait_for_uploads": True},
    {"task": "read this", "auto_start": False},
])
def test_an_unstarted_create_with_no_model_is_503_and_creates_nothing(monkeypatch, body):
    client, created = _client(monkeypatch, llm=None)
    resp = client.post("/api/task/sessions", json=body)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "no_model"
    assert created == []


def test_an_unstarted_create_with_a_model_goes_on(monkeypatch):
    client, created = _client(monkeypatch, llm=object())
    body = {"task": "read this", "auto_start": False, "wait_for_uploads": True}
    resp = client.post("/api/task/sessions", json=body)
    assert resp.status_code == 200 and resp.json()["session_id"] == "sess-1"
    assert created == [body]


def test_a_refused_line_to_a_chat_carries_the_code(monkeypatch):
    """The in-process message route flattened the 503 detail into a string."""
    import asyncio
    from fastapi import HTTPException
    import webview.server as server
    import api.task_http_api as thp

    async def refuse(*a, **k):
        raise HTTPException(status_code=503, detail={"code": "no_model", "message": "x"})

    monkeypatch.setattr(thp, "send_user_message", refuse)
    req = SimpleNamespace(state=SimpleNamespace())
    resp = asyncio.run(server._send_message_in_process(
        req, "abcd1234", object(), "hi", "comment", {}, None, "u1", "u1"))
    assert resp.status_code == 503
    import json
    body = json.loads(resp.body)
    assert body["code"] == "no_model" and body["success"] is False
