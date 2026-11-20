"""The two-service send proxy passes a structured refusal through (2026-11-20).

``POST /api/session/{id}/messages`` proxies to the :9000 API when no task agent
lives in this process. An upstream 409 used to become ``{error}`` and drop
``detail.owner_pid``; an upstream 503 ``no_model`` went to the 5xx retry branch
(three tries, then "Server error: 503" with no code), so ``transcript.js`` drew
a generic error instead of the no-model note.
"""
import asyncio
import json

import httpx
import pytest


class _FakeRequest:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.cookies = {}

    async def json(self):
        return self._body


def _drive(monkeypatch, upstream: httpx.Response):
    import webview.server as server
    calls = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            calls.append(url)
            return upstream

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(server, "_check_session_ownership", lambda req, sid: (True, "u1", "u1"))
    monkeypatch.setattr(server, "_in_process_task_agent", lambda: None)
    monkeypatch.setattr(server, "_api_proxy_auth_headers", lambda req: {})

    async def _no_command(*a, **k):
        return None

    monkeypatch.setattr(server, "_maybe_handle_console_command", _no_command)
    import webview.chat_open as chat_open
    monkeypatch.setattr(chat_open, "terminal_chat_refusal", lambda sid: None)

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    resp = asyncio.run(server.send_message_to_session("sess-1", _FakeRequest({"text": "hello"})))
    return resp, calls


def test_a_409_keeps_owner_pid(monkeypatch):
    body = {"detail": {"code": "live_elsewhere", "owner_pid": 4242}}
    resp, calls = _drive(monkeypatch, httpx.Response(409, json=body))
    assert resp.status_code == 409
    assert json.loads(resp.body) == body
    assert len(calls) == 1


def test_a_503_no_model_is_passed_through_and_not_retried(monkeypatch):
    body = {"detail": {"code": "no_model", "message": "no language model in this process"}}
    resp, calls = _drive(monkeypatch, httpx.Response(503, json=body))
    assert resp.status_code == 503
    assert json.loads(resp.body)["detail"]["code"] == "no_model"
    assert len(calls) == 1, "a no_model 503 is an answer, never retried"


def test_a_plain_500_still_retries(monkeypatch):
    resp, calls = _drive(monkeypatch, httpx.Response(500, text="boom"))
    assert resp.status_code == 503
    assert len(calls) == 3


@pytest.mark.parametrize("status,body", [(400, {"detail": "bad"}), (422, {"error": "x"})])
def test_a_string_detail_keeps_the_old_shape(monkeypatch, status, body):
    resp, calls = _drive(monkeypatch, httpx.Response(status, json=body))
    assert resp.status_code == status
    out = json.loads(resp.body)
    assert out["success"] is False and out["error"]
