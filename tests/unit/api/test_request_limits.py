import asyncio

import pytest

from api.request_limits import RequestBodyLimitMiddleware


async def invoke(chunks, headers=(), *, maximum=8, delay=0):
    received = []
    sent = []
    pending = list(chunks)

    async def receive():
        if delay:
            await asyncio.sleep(delay)
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        while True:
            message = await receive()
            received.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestBodyLimitMiddleware(app, max_bytes=maximum, timeout=0.02)
    await middleware({"type": "http", "headers": headers}, receive, send)
    return sent[0]["status"], b"".join(received)


@pytest.mark.asyncio
async def test_chunked_overflow_never_reaches_parser():
    assert await invoke([b"12345", b"67890"]) == (413, b"")


@pytest.mark.asyncio
async def test_large_declared_body_refused_without_reading():
    assert await invoke([], [(b"content-length", b"999999")]) == (413, b"")


@pytest.mark.asyncio
async def test_exact_boundary_and_empty_body_replayed():
    assert await invoke([b"1234", b"5678"]) == (200, b"12345678")
    assert await invoke([b""]) == (200, b"")


@pytest.mark.asyncio
async def test_total_body_read_deadline():
    assert await invoke([b"1"], delay=0.05) == (408, b"")


@pytest.mark.asyncio
async def test_invalid_lengths_are_rejected():
    assert await invoke([], [(b"content-length", b"-1")]) == (400, b"")
    assert await invoke([], [(b"content-length", b"x")]) == (400, b"")


@pytest.mark.asyncio
async def test_saturated_upload_refused_and_cancelled_upload_releases_slot():
    started = asyncio.Event()
    sent = []

    async def receive():
        started.set()
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    async def app(*args):
        pytest.fail("incomplete upload reached application")

    middleware = RequestBodyLimitMiddleware(app, concurrency=1, timeout=5)
    scope = {"type": "http", "headers": []}
    first = asyncio.create_task(middleware(scope, receive, send))
    await started.wait()
    try:
        await middleware(scope, receive, send)
        assert sent[0]["status"] == 503
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert not middleware.slots.locked()


@pytest.mark.asyncio
async def test_spool_creation_failure_releases_capacity(monkeypatch):
    import api.request_limits as limits
    sent = []

    def fail(**kwargs):
        raise OSError("temporary storage unavailable")

    async def unexpected(*args):
        pytest.fail("failed spool reached downstream")

    async def send(message):
        sent.append(message)

    monkeypatch.setattr(limits.tempfile, "SpooledTemporaryFile", fail)
    middleware = RequestBodyLimitMiddleware(unexpected, concurrency=1)
    await middleware({"type": "http", "headers": []}, unexpected, send)
    assert sent[0]["status"] == 503
    assert not middleware.slots.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize('declared', [b'1', b'3', b'+2', b' 2', b'2 '])
async def test_length_mismatch_or_nondecimal_is_refused(declared):
    assert await invoke([b'12'], [(b'content-length', declared)]) == (400, b'')


@pytest.mark.asyncio
async def test_unconsumed_spool_retains_capacity_and_cancellation_releases():
    entered = asyncio.Event()
    sent = []
    async def receive():
        return {'type': 'http.request', 'body': b'x', 'more_body': False}
    async def send(message):
        sent.append(message)
    async def app(scope, receive, send):
        entered.set()
        await asyncio.Event().wait()
    middleware = RequestBodyLimitMiddleware(app, concurrency=1)
    scope = {'type': 'http', 'headers': []}
    first = asyncio.create_task(middleware(scope, receive, send))
    await entered.wait()
    try:
        await middleware(scope, receive, send)
        assert sent[0]['status'] == 503
        assert middleware.slots.locked()
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert not middleware.slots.locked()


@pytest.mark.asyncio
async def test_consumed_body_frees_slot_before_streaming_handler_finishes():
    consumed = asyncio.Event()
    async def receive():
        return {'type': 'http.request', 'body': b'x', 'more_body': False}
    async def app(scope, receive, send):
        assert (await receive())['body'] == b'x'
        consumed.set()
        await asyncio.Event().wait()
    middleware = RequestBodyLimitMiddleware(app, concurrency=1)
    first = asyncio.create_task(middleware({'type': 'http', 'headers': []}, receive, None))
    await consumed.wait()
    assert not middleware.slots.locked()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert middleware.slots._value == 1


@pytest.mark.asyncio
async def test_bodyless_stream_does_not_hold_upload_capacity():
    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    async def app(scope, receive, send):
        # GET stream handlers may never read a request body.
        assert not middleware.slots.locked()
        assert await receive() == {'type': 'http.request', 'body': b'', 'more_body': False}
    middleware = RequestBodyLimitMiddleware(app, concurrency=1)
    await middleware({'type': 'http', 'headers': []}, receive, None)
    assert middleware.slots._value == 1
