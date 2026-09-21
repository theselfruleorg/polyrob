"""Prefix identity of an LLM request — `prefix_sha` / `tools_sha` (2026-09-20).

Post-057 the money-rail first call missed the provider prompt cache on 8 of 11
sessions, and nothing in the tree could say whether the bytes that reach the
provider are the SAME bytes run to run (a stable prefix on a cold replica) or
DIFFERENT bytes (something session-specific early in the request). No request
bytes are persisted, so the question was an inference. These two short digests
— the first system message's content and the serialised tools list — are
stamped onto the client at request time and copied into the usage record's
metadata at billing time, so `usage_records` can group first-call cache hits
by prefix identity. A digest is metadata: a failure to compute one never
touches the request.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

_PREFIX_ATTR = "_polyrob_prefix_sha"
_TOOLS_ATTR = "_polyrob_tools_sha"


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def compute_stamps(request_params: Dict[str, Any]) -> Dict[str, str]:
    """`{"prefix_sha", "tools_sha"}` for an OpenAI-shaped request; a missing
    part is simply absent from the dict (never a digest of nothing)."""
    out: Dict[str, str] = {}
    try:
        for m in request_params.get("messages") or []:
            if isinstance(m, dict) and m.get("role") == "system":
                content = m.get("content")
                if not isinstance(content, str):
                    content = json.dumps(content, sort_keys=True, default=str)
                out["prefix_sha"] = _sha(content)
                break
        tools = request_params.get("tools")
        if tools:
            out["tools_sha"] = _sha(json.dumps(tools, sort_keys=True, default=str))
    except Exception:
        return out
    return out


def stamp_client(client: Any, request_params: Dict[str, Any]) -> Dict[str, str]:
    """Compute and remember the stamps on *client* for the call being made."""
    stamps = compute_stamps(request_params)
    try:
        setattr(client, _PREFIX_ATTR, stamps.get("prefix_sha"))
        setattr(client, _TOOLS_ATTR, stamps.get("tools_sha"))
    except Exception:
        pass
    return stamps


def read_stamps(llm: Any) -> Dict[str, str]:
    """The stamps of the last call on *llm* (an adapter or a raw client), for
    the billing record. Empty when the provider path does not stamp."""
    client = getattr(llm, "_client", None) or llm
    out: Dict[str, str] = {}
    for key, attr in (("prefix_sha", _PREFIX_ATTR), ("tools_sha", _TOOLS_ATTR)):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            out[key] = value
    return out
