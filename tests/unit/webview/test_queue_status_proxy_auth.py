"""WS10: the queue-status proxy hop dropped the caller's credential, so in the
two-service shape the task API answered 401 and the console always read
`error`. It forwards `_api_proxy_auth_headers` like the chat-send hop."""
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request


@pytest.mark.asyncio
async def test_queue_status_proxy_forwards_the_credential(monkeypatch):
    from webview import server
    monkeypatch.setattr(server, "TASK_ROUTER_MOUNTED", False)
    monkeypatch.setattr(server, "pm", lambda: SimpleNamespace(clean_session_id=lambda sid: sid))
    seen = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"queued_messages": 2, "agent_status": "running"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, **k):
            seen.append(headers or {})
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    request = Request({"type": "http", "path": "/api/session/s1/queue-status",
                       "headers": [(b"authorization", b"Bearer tok-123")]})
    response = await server.get_queue_status("s1", request)
    assert json.loads(response.body)["agent_status"] == "running"
    assert seen == [{"Authorization": "Bearer tok-123"}]
