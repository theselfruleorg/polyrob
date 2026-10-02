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


def _text(value: Any) -> str:
    """A stable string for any system-prompt shape: a plain `str`, Anthropic's
    list of content blocks, or anything else JSON-serialisable."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _system_of(request_params: Dict[str, Any], system: Any = None) -> Any:
    """The system prompt of a request, whatever shape the provider uses.

    F20 (2026-09-22): the original was OpenAI-shaped only — it scanned
    `messages` for `role == "system"`. Anthropic puts the system prompt in a
    top-level `system` key (a `str` OR a list of content blocks) and the
    Responses API calls it `instructions`, so on both of those paths the stamp
    silently came back empty and the billing record could not say whether two
    calls shared a prefix — which is the whole question the stamp exists to
    answer.

    Precedence: an explicit `system=` argument, then `system`, then
    `instructions`, then the first system-role message.
    """
    if system is not None:
        return system
    for key in ("system", "instructions"):
        value = request_params.get(key)
        if value:
            return value
    for m in request_params.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "system":
            return m.get("content")
    return None


def compute_stamps(request_params: Dict[str, Any],
                   system: Any = None) -> Dict[str, str]:
    """`{"prefix_sha", "tools_sha"}` for a request on ANY provider shape; a
    missing part is simply absent from the dict (never a digest of nothing)."""
    out: Dict[str, str] = {}
    try:
        prefix = _system_of(request_params, system)
        if prefix:
            out["prefix_sha"] = _sha(_text(prefix))
        tools = request_params.get("tools")
        if tools:
            out["tools_sha"] = _sha(json.dumps(tools, sort_keys=True, default=str))
    except Exception:
        return out
    return out


def stamp_client(client: Any, request_params: Dict[str, Any],
                 system: Any = None) -> Dict[str, str]:
    """Compute and remember the stamps on *client* for the call being made.

    ⚠️ *client* is the POLYROB `LLMClient` (i.e. `self` inside a client method),
    not the vendor SDK object it wraps — `read_stamps` looks the stamps up
    through `llm._client`, which is that wrapper.
    """
    stamps = compute_stamps(request_params, system)
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
