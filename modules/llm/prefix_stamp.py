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


def _field(obj: Any, name: str) -> Any:
    """`obj[name]` for a dict, else the attribute, else the pydantic extra."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(name)
    return value


def response_stamps(response: Any) -> Dict[str, Any]:
    """What the provider's RESPONSE says about the call (2026-10-14, D-2/D-3).

    * ``upstream`` — the OpenRouter field ``provider``: which upstream actually
      served the call (DeepInfra, Novita, ...). For 12 days prod paid ×2 the
      catalog price and nothing could say which upstream charged it.
    * ``reasoning_tokens`` — ``usage.completion_tokens_details.reasoning_tokens``
      (Chat Completions) or ``usage.output_tokens_details.reasoning_tokens``
      (Responses). It was read and only logged, so nothing could verify that
      ``OPENROUTER_REASONING_ENABLED=false`` holds on a model.

    Either key is absent when the response does not carry it. Never raises.
    """
    out: Dict[str, Any] = {}
    try:
        upstream = _field(response, "provider")
        if isinstance(upstream, str) and upstream.strip():
            out["upstream"] = upstream.strip()[:64]
        usage = _field(response, "usage")
        for details_key in ("completion_tokens_details", "output_tokens_details"):
            reasoning = _field(_field(usage, details_key), "reasoning_tokens")
            if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning > 0:
                out["reasoning_tokens"] = reasoning
                break
    except Exception:
        return out
    return out


_CONTEXT_ATTR = "_polyrob_context_stamps"
_CONTEXT_SLOTS = (("foundation", "ctx_foundation"), ("hmem", "ctx_hmem"),
                  ("conversation", "ctx_conversation"), ("ephemeral", "ctx_ephemeral"),
                  ("tool_schemas", "ctx_schemas"))


def context_stamps(usage: Any) -> Dict[str, Any]:
    """Flatten a ``MessageManager.context_usage()`` reading into usage-record
    metadata keys (D-7): the slot sizes, ``ctx_limit``, ``ctx_pct`` and
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


def stamp_context(llm: Any, usage: Any) -> Dict[str, Any]:
    """Remember the context reading of the agent step about to call *llm*
    (D-7). The NEXT ``read_stamps(llm)`` copies it into the usage record and
    clears it, so a later call that was not stamped never inherits it.

    The hook for the agent step: call it with
    ``self.message_manager.context_usage()`` right before ``llm.ainvoke``/
    ``llm.astream``; the billing site's ``read_stamps`` does the rest.
    """
    stamps = context_stamps(usage)
    client = getattr(llm, "_client", None) or llm
    try:
        setattr(client, _CONTEXT_ATTR, stamps or None)
    except Exception:
        pass
    return stamps


def read_stamps(llm: Any, include_context: bool = True) -> Dict[str, Any]:
    """The stamps of the last call on *llm* (an adapter or a raw client), for
    the billing record. Empty when the provider path does not stamp.

    Keys: ``prefix_sha``/``tools_sha`` (request identity), ``upstream``/
    ``reasoning_tokens`` (read from the client's ``last_response``), and — when
    *include_context* and the agent step called :func:`stamp_context` — the
    ``ctx_*`` slot sizes, consumed by this read. An aux caller passes
    ``include_context=False`` so it neither records nor consumes the step's.
    """
    client = getattr(llm, "_client", None) or llm
    out: Dict[str, Any] = {}
    for key, attr in (("prefix_sha", _PREFIX_ATTR), ("tools_sha", _TOOLS_ATTR)):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            out[key] = value
    out.update(response_stamps(getattr(client, "last_response", None)))
    if include_context:
        ctx = getattr(client, _CONTEXT_ATTR, None)
        if isinstance(ctx, dict) and ctx:
            out.update(ctx)
            try:
                setattr(client, _CONTEXT_ATTR, None)
            except Exception:
                pass
    return out
