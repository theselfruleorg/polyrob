"""API-11: DoS bounds on the HTTP API — IPv6 /64 rate keys, SIWE nonce
issuance, the A2A list page size, and live SSE streams per caller."""
import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("ip,key", [
    ("203.0.113.7", "203.0.113.7"),
    ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
    ("::ffff:198.51.100.4", "198.51.100.4"),
    (None, None),
    ("not-an-ip", "not-an-ip"),
])
def test_ipv6_clients_share_their_64(ip, key):
    from api.dependencies import rate_key_for_ip
    assert rate_key_for_ip(ip) == key


def test_body_slot_key_aggregates_ipv6():
    from api import request_limits
    scope = {"type": "http", "client": ("2001:db8:9:9:1::5", 1234), "headers": [],
             "method": "POST", "path": "/x", "query_string": b"", "server": ("t", 80),
             "scheme": "http"}
    assert request_limits._client_key(scope) == "2001:db8:9:9::/64"


def test_nonce_issuance_is_rate_limited(monkeypatch):
    from fastapi import HTTPException
    from core.rate_limit import SlidingWindowLimiter
    import api.auth_endpoints as ae
    monkeypatch.setattr(ae, "_NONCE_LIMITER", SlidingWindowLimiter(max_calls=2, window_seconds=60))
    req = SimpleNamespace(client=SimpleNamespace(host="2001:db8::1"), headers={})
    ae._nonce_rate_refusal(req)
    ae._nonce_rate_refusal(SimpleNamespace(client=SimpleNamespace(host="2001:db8::2"),
                                           headers={}))
    with pytest.raises(HTTPException) as e:
        ae._nonce_rate_refusal(SimpleNamespace(client=SimpleNamespace(host="2001:db8::3"),
                                               headers={}))
    assert e.value.status_code == 429


@pytest.mark.asyncio
async def test_list_page_size_is_clamped():
    from api.a2a.task_handler import A2ATaskHandler, MAX_LIST_PAGE_SIZE
    h = object.__new__(A2ATaskHandler)
    sessions = [{"id": f"s{i}", "user_id": "u", "created_at": str(i)} for i in range(250)]
    h._get_session_manager = lambda: SimpleNamespace(get_all_sessions=lambda: sessions)

    async def get_task(sid, history_length=None):
        return sid
    h.get_task = get_task
    h.logger = SimpleNamespace(warning=lambda *a, **k: None)
    tasks, nxt = await h.list_tasks("u", page_size=10_000)
    assert len(tasks) == MAX_LIST_PAGE_SIZE and nxt == str(MAX_LIST_PAGE_SIZE)


@pytest.mark.asyncio
async def test_live_streams_per_caller_are_capped(monkeypatch):
    from api.a2a import streaming
    monkeypatch.setattr(streaming, "MAX_STREAMS_PER_USER", 2)
    monkeypatch.setattr(streaming, "_ACTIVE_STREAMS", {})
    gate = asyncio.Event()

    async def live():
        yield "event: open\n\n"
        await gate.wait()

    held = [streaming.bounded_stream("u1", live()) for _ in range(2)]
    assert [await g.__anext__() for g in held] == ["event: open\n\n"] * 2
    refused = streaming.bounded_stream("u1", live())
    msg = await refused.__anext__()
    assert "Too many open task streams" in msg
    other = streaming.bounded_stream("u2", live())
    assert await other.__anext__() == "event: open\n\n"
    for g in held + [other]:
        await g.aclose()
    assert streaming._ACTIVE_STREAMS == {}
