"""Observe paid JSON and SSE outcomes without consuming or replacing their wire bytes."""
import json

MAX_OUTCOME_BYTES = 1024 * 1024


def failed_json(raw: bytes) -> bool:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(value, dict) and (
        value.get("success") is False
        or value.get("error") not in (None, False, "", {}, [])
        or value.get("status") in ("failed", "error")
    )


async def observe_paid_response(response, payment_id: str) -> None:
    """Mark explicit application failures, including HTTP-200 JSON-RPC errors."""
    from modules.x402.x402_integration import mark_payment_refund_due

    if not 200 <= response.status_code < 300:
        return
    kind = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if kind == "text/event-stream":
        _observe_event_stream(response, payment_id)
        return
    if kind != "application/json":
        return
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        if len(body) <= MAX_OUTCOME_BYTES and failed_json(body):
            await mark_payment_refund_due(payment_id)
        return
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        return

    async def observed():
        collected = bytearray()
        overflow = False
        try:
            async for chunk in iterator:
                raw = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                if not overflow:
                    if len(collected) + len(raw) > MAX_OUTCOME_BYTES:
                        collected.clear()
                        overflow = True
                    else:
                        collected.extend(raw)
                yield chunk
        except Exception:
            await mark_payment_refund_due(payment_id)
            raise
        if not overflow and failed_json(collected):
            await mark_payment_refund_due(payment_id)

    response.body_iterator = observed()


def failed_sse_frame(frame: str) -> bool:
    """One SSE event whose ``data:`` payload is a failure (an ``error`` object).

    A streamed turn that fails AFTER its 200 headers carries the failure in the
    body (the `/v1` ``agent_turn_failed`` frame); its status code says nothing."""
    data = "\n".join(line[5:].lstrip() for line in frame.splitlines()
                     if line.startswith("data:"))
    if not data or data == "[DONE]":
        return False
    return failed_json(data.encode("utf-8"))


def _observe_event_stream(response, payment_id: str) -> None:
    """Pass every chunk through unchanged; mark the payment refund-due once when
    an event carries a failure or the stream itself raises."""
    from modules.x402.x402_integration import mark_payment_refund_due

    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        return

    async def observed():
        pending = ""
        marked = False
        try:
            async for chunk in iterator:
                if not marked:
                    text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)
                    *frames, pending = (pending + text).replace("\r\n", "\n").split("\n\n")
                    if len(pending) > MAX_OUTCOME_BYTES:
                        pending = ""
                    if any(failed_sse_frame(f) for f in frames):
                        marked = True
                        await mark_payment_refund_due(payment_id)
                yield chunk
        except Exception:
            if not marked:
                await mark_payment_refund_due(payment_id)
            raise
        if not marked and pending and failed_sse_frame(pending):
            await mark_payment_refund_due(payment_id)

    response.body_iterator = observed()
