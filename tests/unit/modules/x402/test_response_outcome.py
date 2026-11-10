from unittest.mock import AsyncMock

import pytest
from starlette.responses import JSONResponse, StreamingResponse

from modules.x402.response_outcome import observe_paid_response


@pytest.fixture
def refund(monkeypatch):
    callback = AsyncMock()
    monkeypatch.setattr('modules.x402.x402_integration.mark_payment_refund_due', callback)
    return callback


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,failed', [
    ({'success': False, 'text': 'unavailable'}, True),
    ({'jsonrpc': '2.0', 'error': {'code': -32603}}, True),
    ({'status': 'failed'}, True),
    ({'success': True, 'error': None}, False),
    ({'text': '{"success": false}'}, False),
])
async def test_direct_json_outcome(payload, failed, refund):
    response = JSONResponse(payload)
    before = response.body
    await observe_paid_response(response, 'payment')
    assert response.body == before
    assert refund.await_count == int(failed)


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [1, 2, 7, 1000])
async def test_streamed_json_failure_is_refundable_without_changing_wire(size, refund):
    raw = b'{"success":false,"text":"unavailable"}'
    async def chunks():
        for i in range(0, len(raw), size):
            yield raw[i:i + size]
    response = StreamingResponse(chunks(), media_type='application/json')
    await observe_paid_response(response, 'payment')
    assert refund.await_count == 0
    wire = b''.join([chunk async for chunk in response.body_iterator])
    assert wire == raw
    refund.assert_awaited_once_with('payment')


@pytest.mark.asyncio
async def test_successful_large_json_is_streamed_with_bounded_inspection(refund, monkeypatch):
    monkeypatch.setattr('modules.x402.response_outcome.MAX_OUTCOME_BYTES', 8)
    raw = b'{"success":true,"text":"a large payload"}'
    async def chunks():
        yield raw[:7]
        yield raw[7:]
    response = StreamingResponse(chunks(), media_type='application/json')
    await observe_paid_response(response, 'payment')
    assert b''.join([chunk async for chunk in response.body_iterator]) == raw
    refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_broken_paid_json_stream_marks_refund(refund):
    async def chunks():
        yield b'{'
        raise RuntimeError('downstream failed')
    response = StreamingResponse(chunks(), media_type='application/json')
    await observe_paid_response(response, 'payment')
    with pytest.raises(RuntimeError, match='downstream'):
        [chunk async for chunk in response.body_iterator]
    refund.assert_awaited_once_with('payment')


def _sse(*frames):
    return "".join(f"data: {f}\n\n" for f in frames).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [1, 5, 4096])
async def test_streamed_sse_error_frame_is_refundable_without_changing_wire(size, refund):
    """EP-2: a /v1 turn that fails after its 200 headers sends an `error` frame;
    the payment used to stay settled with no refund mark."""
    raw = (b": keep-alive\n\n"
           + _sse('{"error":{"message":"agent turn failed","code":"agent_turn_failed"}}',
                  '[DONE]'))

    async def chunks():
        for i in range(0, len(raw), size):
            yield raw[i:i + size]
    response = StreamingResponse(chunks(), media_type='text/event-stream')
    await observe_paid_response(response, 'payment')
    wire = b''.join([chunk async for chunk in response.body_iterator])
    assert wire == raw
    refund.assert_awaited_once_with('payment')


@pytest.mark.asyncio
async def test_successful_sse_stream_is_not_refunded(refund):
    raw = _sse('{"id":"c","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
               '{"id":"c","choices":[],"usage":{"total_tokens":3}}', '[DONE]')

    async def chunks():
        yield raw
    response = StreamingResponse(chunks(), media_type='text/event-stream')
    await observe_paid_response(response, 'payment')
    assert b''.join([chunk async for chunk in response.body_iterator]) == raw
    assert refund.await_count == 0
