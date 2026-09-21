"""The ONE OpenAI-shaped error envelope for the ``/v1`` surface.

Invariant: every error a caller can receive on a ``/v1`` path is
``{"error": {"message", "type", "code", "param"}}``. An OpenAI SDK client reads
``error.message`` and ``error.type``; POLYROB's native ``{"error": "..."}``
shape makes the SDK report "unknown error" and hides the reason (B18).

The handlers in ``api/app.py`` route through here, so a ``/v1`` error raised
anywhere — a dependency, a validation failure, an unhandled exception —
arrives in the same shape.
"""
from typing import Any, Optional

from fastapi.responses import JSONResponse

#: Path prefix that owns this envelope.
OPENAI_COMPAT_PREFIX = "/v1/"

#: status code -> OpenAI `error.type`
_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "insufficient_quota",
    403: "permission_error",
    404: "not_found_error",
    409: "invalid_request_error",
    413: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
}


def is_openai_compat_path(path: str) -> bool:
    """Whether ``path`` is served by the OpenAI-compatible surface."""
    return bool(path) and (path == "/v1" or path.startswith(OPENAI_COMPAT_PREFIX))


def openai_error_body(
    status_code: int,
    message: Any,
    *,
    code: Optional[str] = None,
    param: Optional[str] = None,
    details: Any = None,
) -> dict:
    """Build the OpenAI error body. ``message`` is always rendered as a string."""
    err_type = _TYPE_BY_STATUS.get(int(status_code), "api_error")
    body = {
        "error": {
            "message": message if isinstance(message, str) else str(message),
            "type": err_type,
            "code": code or err_type,
            "param": param,
        }
    }
    if details is not None:
        body["error"]["details"] = details
    return body


def openai_error_response(
    status_code: int,
    message: Any,
    *,
    code: Optional[str] = None,
    param: Optional[str] = None,
    details: Any = None,
) -> JSONResponse:
    """The OpenAI error body as a ``JSONResponse``."""
    return JSONResponse(
        status_code=status_code,
        content=openai_error_body(
            status_code, message, code=code, param=param, details=details
        ),
    )


def _message_from(payload) -> Optional[str]:
    """Best readable message out of whatever shape a gate produced."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return err.get("message") or err.get("detail")
        for key in ("error", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    elif isinstance(payload, str) and payload.strip():
        return payload
    return None


def already_openai_shaped(payload) -> bool:
    """Whether ``payload`` is already ``{"error": {"message", "type", …}}``."""
    return (isinstance(payload, dict)
            and isinstance(payload.get("error"), dict)
            and "type" in payload["error"])


class OpenAICompatErrorMiddleware:
    """Rewrite any ``/v1`` error body into the OpenAI envelope.

    The exception handlers in ``api/app.py`` only see errors that reach the
    ROUTE. A refusal from a MIDDLEWARE — ``AuthenticationMiddleware``'s 401,
    the fallback gate's 401/503, the body-limit 413 — returns its own
    ``JSONResponse`` and never passes through them, so an OpenAI SDK client
    hitting a gated instance read ``{"error": "Unauthorized", "code": …}``,
    where ``error`` is a STRING, and reported "unknown error" with no reason.
    Registered OUTERMOST so it sees every refusal, whoever produced it.

    ⚠️ **402 is left alone.** On ``/v1/chat/completions`` a 402 carries the
    x402 payment CHALLENGE, which a paying client parses to build its payment.
    Reshaping it would break the payment rail to satisfy a client that is not
    paying anyway.
    """

    #: The one status whose body belongs to another protocol.
    PASSTHROUGH_STATUSES = frozenset({402})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not is_openai_compat_path(
                scope.get("path") or ""):
            await self.app(scope, receive, send)
            return

        import json as _json

        state = {"status": 200, "headers": [], "chunks": [], "rewrite": False}

        async def _send(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = message.get("headers") or []
                state["rewrite"] = (
                    message["status"] >= 400
                    and message["status"] not in self.PASSTHROUGH_STATUSES
                )
                if not state["rewrite"]:
                    await send(message)
                return
            if message["type"] == "http.response.body":
                if not state["rewrite"]:
                    await send(message)
                    return
                state["chunks"].append(message.get("body", b"") or b"")
                if message.get("more_body"):
                    return
                raw = b"".join(state["chunks"])
                try:
                    payload = _json.loads(raw.decode("utf-8") or "null")
                except Exception:
                    payload = raw.decode("utf-8", "replace")
                if already_openai_shaped(payload):
                    body = raw
                else:
                    body = _json.dumps(openai_error_body(
                        state["status"],
                        _message_from(payload) or "Request failed",
                    )).encode("utf-8")
                keep = [(k, v) for k, v in state["headers"]
                        if k.lower() not in (b"content-length", b"content-type")]
                keep.append((b"content-type", b"application/json"))
                keep.append((b"content-length", str(len(body)).encode()))
                await send({"type": "http.response.start",
                            "status": state["status"], "headers": keep})
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        await self.app(scope, receive, _send)
