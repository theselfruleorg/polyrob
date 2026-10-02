"""Provider-agnostic prompt-cache policy (P1-3).

The review (docs/KIMI_RUNTIME_AND_PROMPT_CONTEXT_REVIEW_2026-06.md) flagged that
prompt caching lived only inside ``anthropic_client`` (+ an OpenAI ``prompt_cache_key``)
with **no central seam** — so every other provider (Gemini, OpenRouter, NVIDIA/Kimi,
DeepSeek) re-paid the full system+tools prefix every step.

This module is that seam: a single place that decides *whether* and *how* a request's
stable prefix should be marked cacheable, keyed by provider/model family. Providers
consult it instead of re-deriving caching ad hoc.

Anthropic and OpenAI keep their existing in-client implementations (already optimal and
on the primary hot path); this module is the canonical policy for the providers that had
none, starting with OpenRouter passthrough. Gemini ``cachedContents`` is the next
implementation behind the same interface.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from core.env import bool_env as _bool_env


def prompt_cache_enabled() -> bool:
    """Global kill-switch for prompt caching. On by default.

    Honors the legacy ``ANTHROPIC_PROMPT_CACHE`` for backward compatibility and the
    provider-agnostic ``LLM_PROMPT_CACHE``; either set to a falsey value disables.
    """
    # Use the SSOT bool parser (already imported) so the falsey set matches the rest
    # of the codebase — the old inline check missed "none" (LLM_PROMPT_CACHE=none read
    # as DISABLED everywhere else but NOT here). P4 finalization.
    for var in ("LLM_PROMPT_CACHE", "ANTHROPIC_PROMPT_CACHE"):
        if not _bool_env(var, True):
            return False
    return True


# Which caching strategy applies when a model is routed through OpenRouter.
#   "breakpoints" -> caller must add Anthropic-style cache_control breakpoints
#   "automatic"   -> provider caches server-side, no request changes needed
#   "none"        -> no caching available
def openrouter_cache_strategy(model_type: Optional[str]) -> str:
    """Classify an OpenRouter model id by its caching mechanism.

    Per OpenRouter's prompt-caching docs: Anthropic (claude) and Google (gemini) models
    require explicit ``cache_control`` breakpoints; OpenAI, DeepSeek and Grok cache
    automatically server-side; everything else has no caching.
    """
    m = (model_type or "").lower()
    if not m:
        return "none"
    if m.startswith("anthropic/") or "claude" in m:
        return "breakpoints"
    if m.startswith("google/") or "gemini" in m:
        return "breakpoints"
    if m.startswith(("openai/", "deepseek/", "x-ai/")) or "grok" in m or "gpt" in m:
        return "automatic"
    if m.startswith("qwen/") or "qwen" in m:
        # OpenRouter lists Qwen (Alibaba Cloud) under automatic prefix caching.
        return "automatic"
    return "none"


# UP-08: API floor for Gemini 2.5-flash/2.5-pro explicit cachedContents.
GEMINI_EXPLICIT_CACHE_MIN_TOKENS = 2048


def provider_cache_strategy(provider: str, model: Optional[str] = None) -> str:
    """How a provider's prompt caching is achieved (UP-08). One place the factory and
    clients consult instead of re-deriving caching ad hoc.

    - "in_client"  -> handled inside the client already (anthropic, openai)
    - "automatic"  -> server-side, no request change (deepseek, nvidia/NIM)
    - "explicit"   -> requires an explicit cache object (gemini cachedContents)
    - "breakpoints"-> requires cache_control markers (openrouter for claude/gemini)
    - "none"       -> no caching available
    """
    p = (provider or "").lower()
    if p == "openrouter":
        return openrouter_cache_strategy(model)   # model-dependent routing
    # S4 (2026-08-29): the per-provider answer is a ProviderSpec field
    # (``cache_strategy``) — one table, no second literal here.
    try:
        from modules.llm.provider_spec import get_spec, Transport
        spec = get_spec(p)
        if spec is not None:
            if spec.cache_strategy:
                return spec.cache_strategy
            # P8b (context-usage audit 2026-08-15): a spec-served provider riding
            # the ANTHROPIC_MESSAGES transport (e.g. zai-coding) INHERITS
            # AnthropicClient's in-client cache_control breakpoints.
            if spec.transport is Transport.ANTHROPIC_MESSAGES:
                return "in_client"
    except Exception:
        pass
    return "none"


def gemini_explicit_cache_enabled() -> bool:
    """Opt-in for Gemini explicit cachedContents. Default OFF — implicit caching is
    already free and needs no code; explicit adds a billed, TTL'd managed-object
    lifecycle, so it ships gated until live cache-hit-verified. Global kill-switch wins.
    """
    if not prompt_cache_enabled():
        return False
    return _bool_env("GEMINI_PROMPT_CACHE", False)


def apply_openrouter_cache_control(
    formatted_messages: List[Dict[str, Any]], model_type: Optional[str]
) -> List[Dict[str, Any]]:
    """Mark the system prefix cacheable for breakpoint-style OpenRouter models.

    For Anthropic/Gemini models routed through OpenRouter, convert the (string) system
    message into a single content block carrying ``cache_control: ephemeral`` so the
    large, stable system+tools prefix is served from cache on repeated calls. A no-op
    for automatic/none strategies, when caching is disabled, or when the OpenRouter
    passthrough is switched off (``OPENROUTER_PROMPT_CACHE``, default ON since
    2026-09-22 — a billing failover to ``anthropic/*`` or ``google/*`` on OpenRouter
    sent no breakpoints at all; ``false`` is the escape).

    Returns the (possibly mutated) message list; never raises.
    """
    if not prompt_cache_enabled():
        return formatted_messages
    if not _bool_env("OPENROUTER_PROMPT_CACHE", True):
        return formatted_messages
    if openrouter_cache_strategy(model_type) != "breakpoints":
        return formatted_messages
    try:
        for msg in formatted_messages:
            if msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str) and content:
                    msg["content"] = [
                        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                    ]
                elif isinstance(content, list) and content:
                    last = content[-1]
                    if isinstance(last, dict):
                        last["cache_control"] = {"type": "ephemeral"}
                break  # one system breakpoint is enough
    except Exception:
        return formatted_messages
    return formatted_messages


def apply_openrouter_tools_cache_control(
    tools: Optional[List[Dict[str, Any]]], model_type: Optional[str]
) -> Optional[List[Dict[str, Any]]]:
    """Mark the LAST tool cacheable for breakpoint-style OpenRouter models (UP-08).

    The system breakpoint (``apply_openrouter_cache_control``) doesn't cover the tools
    array — for OpenRouter, tools are a separate top-level request field placed AFTER
    messages, so a system-only breakpoint re-bills the ~3.7k-token tool schema every
    step. A ``cache_control`` marker on the last tool extends the cached prefix over the
    tools block. No-op unless caching + OPENROUTER_PROMPT_CACHE are on and the model is
    breakpoint-style. Never raises; returns the (possibly mutated) tools list.
    """
    if not tools:
        return tools
    if not prompt_cache_enabled():
        return tools
    if not _bool_env("OPENROUTER_PROMPT_CACHE", True):
        return tools
    if openrouter_cache_strategy(model_type) != "breakpoints":
        return tools
    try:
        if isinstance(tools[-1], dict):
            # L2: never mutate the caller's list — it's the memoized per-provider schema
            # cache shared across models. Copy the list and the tail dict before stamping.
            tools = list(tools)
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    except Exception:
        return tools
    return tools


# ---------------------------------------------------------------------------
# F4 — cache TTL by session class (2026-09-22)
#
# Anthropic's ``cache_control`` takes an optional ``ttl``: the default (the bare
# ``{"type": "ephemeral"}`` marker) is a 5-minute cache, and ``"1h"`` buys a
# one-hour one. An interactive owner turn is bursty — a reply lands minutes
# after the last one and the 5-minute window has often already expired, so the
# whole tools+system prefix is re-paid at full price. A cron/goal run is a
# single burst that never returns, so the longer window would only pay the 2x
# write premium for nothing.
#
# WHICH session gets which ttl is an agents-tier decision
# (``agents.task.session_class.apply_session_cache_ttl``) — ``modules`` may not
# import ``agents``. This module owns the stamp and the reader, exactly like
# ``modules/llm/output_budget.py``.
#
# ⚠️ The ttl rides ONLY the real Anthropic API. The Anthropic-compat seats
# (z.ai / kimi / minimax through ``AnthropicCompatClient``) speak a third-party
# ``/v1/messages`` validator that may 4xx on a key it does not know — the same
# class of breakage as the 422 in ``modules/llm/usage_extract.py:48-50``.
# ---------------------------------------------------------------------------

#: attribute stamped on a client/adapter by :func:`apply_cache_ttl`.
_TTL_ATTR = "_polyrob_cache_ttl"

#: the ttl values Anthropic accepts on ``cache_control``. "5m" is the API
#: default and is expressed as the BARE ephemeral marker (byte-identical to the
#: pre-F4 request), never as an explicit ``"ttl": "5m"`` key.
VALID_CACHE_TTLS = ("5m", "1h")


def cache_ttl_setting() -> str:
    """``ANTHROPIC_CACHE_TTL`` — ``auto`` (default) | ``5m`` | ``1h``.

    ``auto`` defers to the session class. An unknown value falls back to
    ``auto`` (fail-open to today's behaviour), never raises.
    """
    raw = (os.environ.get("ANTHROPIC_CACHE_TTL") or "").strip().lower()
    if raw in VALID_CACHE_TTLS:
        return raw
    return "auto"


def resolve_cache_ttl(autonomous: bool) -> str:
    """The ttl for a session of this class, honouring an explicit override.

    ``auto`` -> ``5m`` for an autonomous (cron/goal/planner) run, ``1h`` for an
    interactive one.
    """
    setting = cache_ttl_setting()
    if setting in VALID_CACHE_TTLS:
        return setting
    return "5m" if autonomous else "1h"


def apply_cache_ttl(llm: Any, ttl: Optional[str]) -> Optional[str]:
    """Stamp the resolved cache ttl onto *llm* (and its wrapped client).

    Idempotent and cheap — re-stamped once a step on purpose, because a provider
    fallback builds a NEW client mid-run. Fail-open: returns None and changes
    nothing when the ttl is not one this API knows.
    """
    if llm is None or ttl not in VALID_CACHE_TTLS:
        return None
    try:
        setattr(llm, _TTL_ATTR, ttl)
        client = getattr(llm, "_client", None)
        if client is not None:
            setattr(client, _TTL_ATTR, ttl)
    except Exception:  # pragma: no cover - defensive
        return None
    return ttl


def cache_ttl_for(client: Any) -> Optional[str]:
    """The ttl to put on this request's ``cache_control`` markers, or None.

    None means "emit the bare ``{"type": "ephemeral"}`` marker" — which is both
    the 5-minute default AND the byte-identical pre-F4 request. Only ``"1h"``
    ever comes back as a value to render.
    """
    if client is None:
        return None
    value = getattr(client, _TTL_ATTR, None)
    if value is None:
        inner = getattr(client, "_client", None)
        value = getattr(inner, _TTL_ATTR, None) if inner is not None else None
    if value == "1h":
        return "1h"
    return None


def cache_control_marker(ttl: Optional[str] = None) -> Dict[str, Any]:
    """The ONE ``cache_control`` value every Anthropic breakpoint uses.

    ``{"type": "ephemeral"}`` by default (byte-identical to pre-F4), plus
    ``"ttl": "1h"`` when the resolved ttl asks for the long window.
    """
    if ttl == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# F12 — where the conversation breakpoints go (2026-09-23)
#
# Anthropic allows FOUR ``cache_control`` breakpoints per request. One is spent
# on the last system block (:func:`_build_cached_system_param`); the other three
# ride in the ``messages`` array. Until F12 they sat on the newest three rows —
# the bytes GUARANTEED to change on the next step — so nothing marked the end of
# the foundation (runtime identity, environment, self/project context, the
# initial task, skills, the tool catalog: the largest static block that travels
# inside ``messages``). After any tail rewrite the three moving markers were
# orphaned and the next request re-wrote the cache from the system marker
# forward.
#
# The placement this module computes:
#   * marker A — the LAST foundation message, so the static block is cached
#     behind a breakpoint that never moves while the session lives;
#   * markers B and C — the last two COMPLETED tool transactions (the
#     ``user`` row carrying ``tool_result`` blocks that closes an
#     ``assistant``/``tool_use`` pair), which is where the transcript is stable
#     once written, with the LAST row always marked so the tail keeps extending
#     the cache.
#
# Every breakpoint in one request carries the SAME marker value (F4 resolves one
# ttl per request), which also satisfies Anthropic's non-increasing-ttl rule.
# ---------------------------------------------------------------------------

#: attribute stamped on a client/adapter by :func:`apply_foundation_len`.
_FOUNDATION_LEN_ATTR = "_polyrob_foundation_len"


def apply_foundation_len(llm: Any, n: Optional[int]) -> Optional[int]:
    """Stamp the WIRE foundation length onto *llm* (and its wrapped client).

    *n* is the number of leading messages in the provider's ``messages`` array
    that are the pinned foundation. Anthropic carries the system prompt in its
    own ``system`` parameter, so the system rows are NOT counted — the agent
    tier computes the number it stamps (``retrieval.get_messages_for_llm``).

    Re-stamped once per step on purpose: a billing failover mints a new client
    mid-run, exactly like :func:`apply_cache_ttl`. Fail-open — returns None and
    changes nothing when *n* is not a usable count.
    """
    if llm is None or not isinstance(n, int) or isinstance(n, bool) or n < 1:
        return None
    try:
        setattr(llm, _FOUNDATION_LEN_ATTR, n)
        client = getattr(llm, "_client", None)
        if client is not None:
            setattr(client, _FOUNDATION_LEN_ATTR, n)
    except Exception:  # pragma: no cover - defensive
        return None
    return n


def foundation_len_for(client: Any) -> Optional[int]:
    """The stamped wire foundation length for this request, or None.

    None means "nobody told us where the foundation ends" and the breakpoint
    placement falls back to the byte-identical pre-F12 tail markers.
    """
    if client is None:
        return None
    value = getattr(client, _FOUNDATION_LEN_ATTR, None)
    if value is None:
        inner = getattr(client, "_client", None)
        value = getattr(inner, _FOUNDATION_LEN_ATTR, None) if inner is not None else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def is_tool_result_message(message: Any) -> bool:
    """Does this Anthropic wire message CLOSE a tool transaction?

    In Anthropic's shape a completed ``assistant``/``tool_use`` pair is closed
    by a ``user`` message whose content blocks are ``tool_result``. That row is
    the endpoint of a transaction: everything up to and including it is settled
    transcript, which is exactly what a breakpoint wants behind it.
    """
    if not isinstance(message, dict):
        return False
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def conversation_breakpoints(
    messages: Optional[List[Dict[str, Any]]],
    foundation_len: Optional[int] = None,
    n: int = 3,
) -> List[int]:
    """Indices in *messages* that should carry a ``cache_control`` marker.

    Pure: no env reads, no mutation, no provider calls.

    Args:
        messages: the provider's ``messages`` array (Anthropic wire shape).
        foundation_len: how many LEADING rows are the pinned foundation. None
            (or < 1) selects the pre-F12 behaviour — the last ``n`` rows,
            byte-identically.
        n: the breakpoint budget for the conversation (3: Anthropic's 4 per
            request minus the one spent on the system block).

    Returns:
        A sorted list of at most ``n`` distinct indices. The last row is always
        included when there is one, so the tail keeps extending the cache.
    """
    total = len(messages or [])
    if total <= 0 or n <= 0:
        return []

    # Pre-F12 fallback: nobody named the foundation, so mark the newest rows.
    if not isinstance(foundation_len, int) or isinstance(foundation_len, bool) or foundation_len < 1:
        return list(range(max(0, total - n), total))

    anchors: List[int] = [min(foundation_len - 1, total - 1)]

    moving = n - len(anchors)
    picks: List[int] = []
    if moving > 0:
        endpoints = [i for i, m in enumerate(messages) if is_tool_result_message(m)]
        picks = endpoints[-moving:]
        # The tail must always be marked, or the newest turn never enters the cache.
        if (total - 1) not in picks:
            picks.append(total - 1)
        # Fewer completed transactions than markers ⇒ fall back to the last rows.
        candidate = total - 1
        while len(picks) < moving and candidate >= 0:
            if candidate not in picks and candidate not in anchors:
                picks.append(candidate)
            candidate -= 1
        picks = sorted(set(picks))[-moving:]

    return sorted(set(anchors) | set(picks))


def mark_conversation_cache(
    messages: Optional[List[Dict[str, Any]]],
    n: int = 3,
    ttl: Optional[str] = None,
    foundation_len: Optional[int] = None,
) -> Any:
    """Copy *messages* and stamp ``cache_control`` on the F12 breakpoints.

    Never mutates the caller's list or any block it holds. Returns the input
    unchanged (same object) when caching is off or there is nothing to mark —
    the kill-switch path stays byte-identical.
    """
    if not prompt_cache_enabled() or not messages:
        return messages

    targets = conversation_breakpoints(messages, foundation_len=foundation_len, n=n)
    if not targets:
        return messages

    marker = cache_control_marker(ttl)
    out = [dict(m) for m in messages]
    for i in targets:
        m = out[i]
        content = m.get("content")
        if isinstance(content, str):
            m["content"] = [{"type": "text", "text": content, "cache_control": dict(marker)}]
        elif isinstance(content, list) and content:
            # copy the last block so we don't mutate the shared dict, then mark it
            content = [dict(b) if isinstance(b, dict) else b for b in content]
            if isinstance(content[-1], dict):
                content[-1] = {**content[-1], "cache_control": dict(marker)}
            m["content"] = content
    return out
