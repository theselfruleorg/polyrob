"""Observe paid JSON outcomes without consuming or replacing their wire bytes."""
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
    if response.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
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
