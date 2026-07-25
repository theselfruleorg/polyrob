"""T2.4 Task 2 — MCPTransport._send_with_auth_retry: 401-retry-once.

Unit-tests the shared base-class helper directly with a stub ``do_send``
(no real transport, no network) — per the plan, this is a new test harness
(none existed before for this shape).
"""
import pytest

from core.exceptions import MCPProtocolError
from tools.mcp.protocol import MCPTransport, _MCPUnauthorized


def _unauthorized(msg: str) -> _MCPUnauthorized:
    return _MCPUnauthorized(MCPProtocolError(msg))


# --- no callback configured: byte-identical to pre-retry behaviour --------------

@pytest.mark.asyncio
async def test_no_callback_401_raises_immediately_without_retry():
    transport = MCPTransport()  # on_auth_refresh defaults to None
    calls = []

    async def do_send():
        calls.append(1)
        raise _unauthorized("status: 401, first")

    with pytest.raises(MCPProtocolError, match="first"):
        await transport._send_with_auth_retry(do_send)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_success_on_first_try_no_retry_attempted():
    transport = MCPTransport()
    calls = []

    async def do_send():
        calls.append(1)

    await transport._send_with_auth_retry(do_send)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_non_401_exception_propagates_unaffected():
    transport = MCPTransport()

    async def do_send():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await transport._send_with_auth_retry(do_send)


# --- callback configured: 401 -> refresh -> retry once --------------------------

@pytest.mark.asyncio
async def test_401_then_200_refreshes_once_and_retries_once():
    refreshed = []

    async def on_auth_refresh():
        refreshed.append(1)
        return "Bearer new-token"

    transport = MCPTransport(on_auth_refresh=on_auth_refresh)
    transport.headers = {"Authorization": "Bearer old-token"}
    calls = []

    async def do_send():
        calls.append(1)
        if len(calls) == 1:
            raise _unauthorized("status: 401")
        # second call: succeeds (no exception)

    await transport._send_with_auth_retry(do_send)

    assert len(calls) == 2
    assert len(refreshed) == 1
    assert transport.headers["Authorization"] == "Bearer new-token"


@pytest.mark.asyncio
async def test_401_then_401_raises_after_exactly_one_retry():
    refreshed = []

    async def on_auth_refresh():
        refreshed.append(1)
        return "Bearer new-token"

    transport = MCPTransport(on_auth_refresh=on_auth_refresh)
    transport.headers = {}
    calls = []

    async def do_send():
        calls.append(1)
        raise _unauthorized(f"status: 401, attempt {len(calls)}")

    with pytest.raises(MCPProtocolError, match="attempt 2"):
        await transport._send_with_auth_retry(do_send)

    assert len(calls) == 2  # exactly one retry, never a second
    assert len(refreshed) == 1  # refreshed exactly once


@pytest.mark.asyncio
async def test_refresh_callback_raising_surfaces_original_401_no_retry():
    async def on_auth_refresh():
        raise RuntimeError("refresh boom")

    transport = MCPTransport(on_auth_refresh=on_auth_refresh)
    transport.headers = {}
    calls = []

    async def do_send():
        calls.append(1)
        raise _unauthorized("status: 401, original")

    with pytest.raises(MCPProtocolError, match="original"):
        await transport._send_with_auth_retry(do_send)
    assert len(calls) == 1  # do_send never retried after a failed refresh


@pytest.mark.asyncio
async def test_refresh_returning_falsy_skips_header_mutation_but_still_retries():
    async def on_auth_refresh():
        return None  # e.g. refresh "succeeded" but produced nothing usable

    transport = MCPTransport(on_auth_refresh=on_auth_refresh)
    transport.headers = {"Authorization": "Bearer old"}
    calls = []

    async def do_send():
        calls.append(1)
        if len(calls) == 1:
            raise _unauthorized("status: 401")

    await transport._send_with_auth_retry(do_send)
    assert len(calls) == 2
    assert transport.headers["Authorization"] == "Bearer old"  # unchanged, no falsy overwrite


@pytest.mark.asyncio
async def test_session_headers_also_updated_when_transport_has_a_session():
    """The streamable transport bakes Authorization into aiohttp session default
    headers at connect() time — mutating self.headers alone would not affect a
    retried request through that same session. The helper must also update
    self.session.headers when present."""

    class _FakeSession:
        def __init__(self):
            self.headers = {"Authorization": "Bearer old"}

    async def on_auth_refresh():
        return "Bearer session-new"

    transport = MCPTransport(on_auth_refresh=on_auth_refresh)
    transport.headers = {"Authorization": "Bearer old"}
    transport.session = _FakeSession()
    calls = []

    async def do_send():
        calls.append(1)
        if len(calls) == 1:
            raise _unauthorized("status: 401")

    await transport._send_with_auth_retry(do_send)
    assert transport.session.headers["Authorization"] == "Bearer session-new"
    assert transport.headers["Authorization"] == "Bearer session-new"
