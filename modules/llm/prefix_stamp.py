"""The stamps of an LLM request.

``prefix_sha`` / ``tools_sha`` / ``foundation_sha`` are short digests of the first system
message, the serialised tools list and the pinned foundation rows. Without them,
``usage_records`` could not say whether two calls sent the same bytes, so a cache miss had
no cause. A digest is metadata: a failure to compute one never touches the request.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from modules.llm.call_scope import note_request_stamps, scoped_foundation_len


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

    OpenAI-shaped requests carry it as the `role == "system"` entry of
    `messages`. Anthropic puts the system prompt in a top-level `system` key
    (a `str` OR a list of content blocks) and the Responses API calls it
    `instructions`. A stamp that missed those two shapes would come back empty
    and the billing record could not say whether two calls shared a prefix —
    which is the whole question the stamp exists to answer.

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


def _row_bytes(row: Any) -> Any:
    """A wire row without its cache markers: a moving marker is not a changed row."""
    if not isinstance(row, dict):
        return row
    content = row.get("content")
    if isinstance(content, list):
        blocks = [{k: v for k, v in b.items() if k != "cache_control"} if isinstance(b, dict) else b
                  for b in content]
        if len(blocks) == 1 and isinstance(blocks[0], dict) and blocks[0].get("type") == "text":
            content = blocks[0].get("text")
        else:
            content = blocks
    return {"role": row.get("role"), "content": content}


def _foundation_rows(request_params: Dict[str, Any]) -> list:
    n = scoped_foundation_len()
    if not n:
        return []
    rows = [m for m in request_params.get("messages") or []
            if not (isinstance(m, dict) and m.get("role") == "system")]
    return [_row_bytes(r) for r in rows[:n]]


def compute_stamps(request_params: Dict[str, Any],
                   system: Any = None) -> Dict[str, str]:
    """The request digests on ANY provider shape; a missing part is simply absent from
    the dict (never a digest of nothing)."""
    out: Dict[str, str] = {}
    try:
        prefix = _system_of(request_params, system)
        if prefix:
            out["prefix_sha"] = _sha(_text(prefix))
        tools = request_params.get("tools")
        if tools:
            out["tools_sha"] = _sha(json.dumps(tools, sort_keys=True, default=str))
        foundation = _foundation_rows(request_params)
        if foundation:
            out["foundation_sha"] = _sha(json.dumps(foundation, sort_keys=True, default=str))
    except Exception:
        return out
    return out


def stamp_request(request_params: Dict[str, Any], system: Any = None) -> Dict[str, str]:
    """Compute the stamps of the request on the wire and give them to this task's call."""
    stamps = compute_stamps(request_params, system)
    note_request_stamps(stamps)
    return stamps


_CONTEXT_SLOTS = (("foundation", "ctx_foundation"), ("hmem", "ctx_hmem"),
                  ("conversation", "ctx_conversation"), ("ephemeral", "ctx_ephemeral"),
                  ("tool_schemas", "ctx_schemas"))


def context_stamps(usage: Any) -> Dict[str, Any]:
    """Flatten a ``MessageManager.context_usage()`` reading into usage-record
    metadata keys: the slot sizes, ``ctx_limit``, ``ctx_pct`` and
    ``ctx_source``. Empty for anything that is not that shape."""
    out: Dict[str, Any] = {}
    if not isinstance(usage, dict):
        return out
    try:
        slots = usage.get("slots") or {}
        for slot, key in _CONTEXT_SLOTS:
            value = slots.get(slot)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = int(value)
        limit = usage.get("limit")
        if isinstance(limit, (int, float)) and limit > 0:
            out["ctx_limit"] = int(limit)
        pct = usage.get("pct")
        if isinstance(pct, (int, float)):
            out["ctx_pct"] = round(float(pct), 1)
        source = usage.get("source")
        if isinstance(source, str) and source:
            out["ctx_source"] = source
    except Exception:
        return out
    return out
