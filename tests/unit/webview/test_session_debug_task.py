"""WS9: the session debug endpoint read the task over a hard-coded
``http://localhost:8008`` with no credential; it now calls the same in-process
reader as ``/api/session/{id}/task`` (``build_session_task``)."""
from types import SimpleNamespace

import pytest
from starlette.requests import Request


@pytest.mark.asyncio
async def test_debug_reads_the_task_in_process(monkeypatch, tmp_path):
    from webview import server
    feed = tmp_path / "s1" / "feed"
    feed.mkdir(parents=True)
    monkeypatch.setattr(server, "pm", lambda: SimpleNamespace(
        clean_session_id=lambda sid: sid, get_feed_dir=lambda sid: feed,
        find_session_root=lambda sid, user_id=None: feed.parent))
    monkeypatch.setattr(server, "_check_session_ownership", lambda req, sid: (True, "o", "o"))
    monkeypatch.setattr(server, "build_session_task",
                        lambda sid: {"task": "write the report", "timestamp": 7})
    hosts = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            hosts.append(url)
            raise RuntimeError("no network in this test")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    request = Request({"type": "http", "path": "/api/session/s1/debug", "headers": []})
    response = await server.api_session_debug("s1", request)
    import json
    body = json.loads(response.body)
    assert body["task"] == {"task": "write the report", "timestamp": 7}
    assert not any("8008" in h for h in hosts)
