"""One reader for an MCP reply over HTTP: plain JSON or SSE-framed (093 B1).

Spec 2025-03-26+ ("Streamable HTTP"): the client POSTs one JSON-RPC message with
``Accept: application/json, text/event-stream``; the server answers EITHER a
plain JSON body OR a ``text/event-stream`` whose events carry JSON-RPC messages
(notifications or server requests may come first; the response is the one whose
``id`` matches). A server that gets no such ``Accept`` header answers 406 —
the 2026-10-28 prod incident (``/mcp add`` of a spec-current server).

Both HTTP transports (``MCPHTTPTransport`` and ``MCPStreamableHTTPTransport``)
read through here, so a probe failure and a live failure say the same thing:
the HTTP status and the first line of the body, never a stack trace.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

#: The header every HTTP MCP POST carries. Without it a Streamable HTTP server
#: answers 406 "Client must accept both application/json and text/event-stream".
ACCEPT = "application/json, text/event-stream"

#: The order ``/mcp add`` tries transports in; the first that answers
#: ``initialize`` is stored. Streamable HTTP first: it is the current spec.
PROBE_ORDER = ("streamable_http", "http", "sse")


def first_body_line(body: Any, limit: int = 200) -> str:
    """The first non-empty line of a response body, cut to ``limit`` chars."""
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def http_failure_text(status: int, body: Any) -> str:
    """``HTTP 406: <first body line>`` — the one shape of an HTTP refusal."""
    line = first_body_line(body)
    return f"HTTP {status}: {line}" if line else f"HTTP {status} (empty body)"


def rpc_payload(message: Any) -> Dict[str, Any]:
    """The JSON-RPC dict for an ``MCPRequest`` / ``MCPNotification`` / ``MCPResponse``.

    Duck-typed on the fields so this module does not import ``protocol``: a
    request has ``method`` and ``id``; a notification has ``method`` and no id;
    a response has ``id`` and ``result``/``error``."""
    out: Dict[str, Any] = {"jsonrpc": getattr(message, "jsonrpc", "2.0")}
    method = getattr(message, "method", None)
    msg_id = getattr(message, "id", None)
    if method:
        if msg_id is not None and not is_notification(message):
            out["id"] = msg_id
        out["method"] = method
        params = getattr(message, "params", None)
        if params:
            out["params"] = params
        return out
    out["id"] = msg_id
    if getattr(message, "result", None) is not None:
        out["result"] = message.result
    if getattr(message, "error", None) is not None:
        out["error"] = message.error
    return out


def is_notification(message: Any) -> bool:
    """True for a message that expects no reply (a JSON-RPC notification)."""
    return type(message).__name__ == "MCPNotification"


def _matches(msg: Any, request_id: Any) -> bool:
    if not isinstance(msg, dict) or ("result" not in msg and "error" not in msg):
        return False
    if request_id is None:
        return True
    if "error" in msg and msg.get("id") is None:
        # JSON-RPC: a server that could not read the request id (a parse or
        # invalid-request error) answers with ``id: null``. One POST carries one
        # request, so that error IS the answer to the pending request.
        return True
    return str(msg.get("id")) == str(request_id)


def _as_messages(data: Any) -> List[Any]:
    return list(data) if isinstance(data, list) else [data]


def pick_reply(messages: Iterable[Any], request_id: Any) -> Optional[Dict[str, Any]]:
    """The JSON-RPC response for ``request_id`` among ``messages``, else None."""
    for msg in messages:
        if _matches(msg, request_id):
            return msg
    return None


def _event_message(data_lines: List[str], request_id: Any) -> Optional[Dict[str, Any]]:
    if not data_lines:
        return None
    try:
        data = json.loads("\n".join(data_lines))
    except (json.JSONDecodeError, ValueError):
        return None
    return pick_reply(_as_messages(data), request_id)


async def read_rpc_reply(response: Any, request_id: Any) -> Optional[Dict[str, Any]]:
    """Read the JSON-RPC response for ``request_id`` from an aiohttp response.

    A ``text/event-stream`` body is read event by event and returns at the
    matching response, so a server that keeps the stream open does not hang the
    call. A plain body is parsed as JSON (one message or a batch). Returns None
    when the body carries no matching response."""
    ctype = (response.headers.get("Content-Type", "") or "").lower()
    if "text/event-stream" not in ctype:
        body = await response.text()
        if not body.strip():
            return None
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            raise ValueError(f"reply is not JSON: {first_body_line(body)}")
        return pick_reply(_as_messages(data), request_id)
    data_lines: List[str] = []
    async for raw in response.content:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            found = _event_message(data_lines, request_id)
            data_lines = []
            if found is not None:
                return found
        elif line.startswith("data:"):
            data_lines.append(line[5:][1:] if line[5:].startswith(" ") else line[5:])
    return _event_message(data_lines, request_id)
