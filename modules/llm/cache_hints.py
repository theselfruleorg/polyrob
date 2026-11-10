"""Provider-agnostic prompt-cache policy.

Prompt caching inside ``anthropic_client`` (+ an OpenAI ``prompt_cache_key``) alone
leaves every other provider (Gemini, OpenRouter, NVIDIA/Kimi, DeepSeek) paying the
full system+tools prefix every step.

This module is the central seam: a single place that decides *whether* and *how* a request's
stable prefix should be marked cacheable, keyed by provider/model family. Providers
consult it instead of re-deriving caching ad hoc.

Anthropic and OpenAI keep their existing in-client implementations (already optimal and
on the primary hot path); this module is the canonical policy for the providers without
an in-client implementation, starting with OpenRouter passthrough. Gemini caches implicitly: no request change.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from core.env import bool_env as _bool_env
from modules.llm.call_scope import scoped_cache_ttl, set_cache_ttl
from modules.llm.model_types import model_family


def prompt_cache_enabled() -> bool:
    """Global kill-switch for prompt caching. On by default.

    Honors the legacy ``ANTHROPIC_PROMPT_CACHE`` for backward compatibility and the
    provider-agnostic ``LLM_PROMPT_CACHE``; either set to a falsey value disables.
    """
    # Use the SSOT bool parser (already imported) so the falsey set matches the rest
    # of the codebase, including "none" (LLM_PROMPT_CACHE=none reads as DISABLED).
    for var in ("LLM_PROMPT_CACHE", "ANTHROPIC_PROMPT_CACHE"):
        if not _bool_env(var, True):
            return False
    return True


# How a model routed through OpenRouter caches, per OpenRouter's prompt-caching docs:
#   "breakpoints" -> caller must add Anthropic-style cache_control breakpoints
#   "automatic"   -> provider caches server-side, no request changes needed
#   "none"        -> no caching available
# A slug's vendor decides, else the model's family.
_OPENROUTER_VENDOR_CACHE = {
    "anthropic": "breakpoints", "google": "breakpoints", "qwen": "breakpoints",
    "openai": "automatic", "deepseek": "automatic", "x-ai": "automatic", "z-ai": "automatic",
    "moonshotai": "automatic",
}
_OPENROUTER_FAMILY_CACHE = {
    "claude": "breakpoints", "gemini": "breakpoints", "qwen": "breakpoints",
    "gpt": "automatic", "deepseek": "automatic", "grok": "automatic", "glm": "automatic",
    "kimi": "automatic",
}


def openrouter_cache_strategy(model_type: Optional[str]) -> str:
    """Classify an OpenRouter model id by its caching mechanism."""
    m = (model_type or "").lower()
    vendor = _OPENROUTER_VENDOR_CACHE.get(m.split("/", 1)[0])
    return vendor or _OPENROUTER_FAMILY_CACHE.get(model_family(m), "none")


def provider_cache_strategy(provider: str, model: Optional[str] = None) -> str:
    """How a provider's prompt caching is achieved. One place the factory and
    clients consult instead of re-deriving caching ad hoc.

    - "in_client"  -> handled inside the client already (anthropic, openai)
    - "automatic"  -> server-side, no request change (deepseek, nvidia/NIM, gemini)
    - "breakpoints"-> requires cache_control markers (openrouter for claude/gemini)
    - "none"       -> no caching available
    """
    p = (provider or "").lower()
    if p == "openrouter":
        return openrouter_cache_strategy(model)   # model-dependent routing
    # The per-provider answer is a ProviderSpec field
    # (``cache_strategy``) — one table, no second literal here.
    try:
        from modules.llm.provider_spec import get_spec, Transport
        spec = get_spec(p)
        if spec is not None:
            if spec.cache_strategy:
                return spec.cache_strategy
            # A spec-served provider riding
            # the ANTHROPIC_MESSAGES transport (e.g. zai-coding) INHERITS
            # AnthropicClient's in-client cache_control breakpoints.
            if spec.transport is Transport.ANTHROPIC_MESSAGES:
                return "in_client"
    except Exception:
        pass
    return "none"


def apply_openrouter_cache_control(
    formatted_messages: List[Dict[str, Any]], model_type: Optional[str]
) -> List[Dict[str, Any]]:
    """Mark the system prefix cacheable for breakpoint-style OpenRouter models.

    For Anthropic/Gemini models routed through OpenRouter, convert the (string) system
    message into a single content block carrying ``cache_control: ephemeral`` so the
    large, stable system+tools prefix is served from cache on repeated calls. A no-op
    for automatic/none strategies or when caching is disabled (always on for OpenRouter,
    so a billing failover to ``anthropic/*`` or ``google/*`` still sends breakpoints).

    Returns the (possibly mutated) message list; never raises.
    """
    if not prompt_cache_enabled():
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
    """Mark the LAST tool cacheable for breakpoint-style OpenRouter models.

    The system breakpoint (``apply_openrouter_cache_control``) doesn't cover the tools
    array — for OpenRouter, tools are a separate top-level request field placed AFTER
    messages, so a system-only breakpoint re-bills the ~3.7k-token tool schema every
    step. A ``cache_control`` marker on the last tool extends the cached prefix over the
    tools block. No-op unless caching is on and the model is
    breakpoint-style. Never raises; returns the (possibly mutated) tools list.
    """
    if not tools:
        return tools
    if not prompt_cache_enabled():
        return tools
    if openrouter_cache_strategy(model_type) != "breakpoints":
        return tools
    try:
        if isinstance(tools[-1], dict):
            # Never mutate the caller's list — it's the memoized per-provider schema
            # cache shared across models. Copy the list and the tail dict before stamping.
            tools = list(tools)
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    except Exception:
        return tools
    return tools


# ---------------------------------------------------------------------------
# Cache TTL by session class
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

#: the ttl values Anthropic accepts on ``cache_control``. "5m" is the API
#: default and is expressed as the BARE ephemeral marker (the API default), never as an explicit ``"ttl": "5m"`` key.
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


def apply_cache_ttl(ttl: Optional[str]) -> Optional[str]:
    """Scope the resolved cache ttl to this task. A cron run and a chat on one client keep
    their own window. Fail-open: returns None and changes nothing when the ttl is not one
    this API knows."""
    if ttl not in VALID_CACHE_TTLS:
        return None
    set_cache_ttl(ttl)
    return ttl


def cache_ttl_for() -> Optional[str]:
    """The ttl to put on this request's ``cache_control`` markers, or None.

    None means "emit the bare ``{"type": "ephemeral"}`` marker", the 5-minute default.
    Only ``"1h"`` ever comes back as a value to render.
    """
    return "1h" if scoped_cache_ttl() == "1h" else None


def cache_control_marker(ttl: Optional[str] = None) -> Dict[str, Any]:
    """The ONE ``cache_control`` value every Anthropic breakpoint uses.

    ``{"type": "ephemeral"}`` by default (the API default), plus
    ``"ttl": "1h"`` when the resolved ttl asks for the long window.
    """
    if ttl == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Where the conversation breakpoints go
#
# Anthropic allows FOUR ``cache_control`` breakpoints per request. One is spent
# on the last system block (:func:`_build_cached_system_param`); the other three
# ride in the ``messages`` array:
#   * marker A — the LAST foundation message, so the static block is cached
#     behind a breakpoint that never moves while the session lives;
#   * markers B and C — the last two COMPLETED tool transactions (the
#     ``user`` row carrying ``tool_result`` blocks that closes an
#     ``assistant``/``tool_use`` pair), which is where the transcript is stable
#     once written. With fewer than two, the newest settled rows fill the budget.
#
# The tail is never marked for its own sake: the rows after the last assistant turn
# include the state row and the H-MEM row, which ride one call and leave, so a marker
# there pays a write premium each step for bytes nothing reads again.
#
# Every breakpoint in one request carries the SAME marker value (one
# ttl is resolved per request), which also satisfies Anthropic's non-increasing-ttl rule. A marker
# never lands on a thinking block or on an empty text block: the API refuses both, so
# the mark moves to the previous block of the row, else the previous row.
# ---------------------------------------------------------------------------

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


_UNMARKABLE_BLOCKS = ("thinking", "redacted_thinking", "tool_addition", "tool_removal")


def _markable_block(block: Any) -> bool:
    if not isinstance(block, dict) or block.get("type") in _UNMARKABLE_BLOCKS:
        return False
    return block.get("type") != "text" or bool(str(block.get("text") or "").strip())


def _can_mark(message: Any) -> bool:
    """Can this row hold a marker? A system row of ``messages`` never does."""
    if not isinstance(message, dict) or message.get("role") == "system":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return isinstance(content, list) and any(_markable_block(b) for b in content)


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
            (or < 1) means a call with no foundation, such as an aux call: the
            newest ``n`` rows.
        n: the breakpoint budget for the conversation (3: Anthropic's 4 per
            request minus the one spent on the system block).

    Returns:
        A sorted list of at most ``n`` distinct indices of rows that can hold a marker.
    """
    rows = messages or []
    total = len(rows)
    if total <= 0 or n <= 0:
        return []
    markable = [i for i, m in enumerate(rows) if _can_mark(m)]

    if not isinstance(foundation_len, int) or isinstance(foundation_len, bool) or foundation_len < 1:
        return markable[-n:]

    anchor = [i for i in markable if i < foundation_len][-1:]
    moving = n - len(anchor)
    picks = [i for i in markable if i not in anchor and is_tool_result_message(rows[i])][-moving:]
    if len(picks) < moving:
        # Fewer completed transactions than markers: the newest settled rows, which end at
        # the last assistant turn.
        last_assistant = max((i for i, m in enumerate(rows)
                              if isinstance(m, dict) and m.get("role") == "assistant"), default=-1)
        settled = [i for i in markable
                   if foundation_len <= i <= last_assistant and i not in picks]
        picks = settled[-(moving - len(picks)):] + picks
    return sorted(set(anchor) | set(picks))


def _marked_content(content: Any, marker: Dict[str, Any]) -> Any:
    if isinstance(content, str):
        return [{"type": "text", "text": content, "cache_control": dict(marker)}]
    blocks = [dict(b) if isinstance(b, dict) else b for b in content]
    for index in range(len(blocks) - 1, -1, -1):
        if _markable_block(blocks[index]):
            blocks[index]["cache_control"] = dict(marker)
            break
    return blocks


def mark_conversation_cache(
    messages: Optional[List[Dict[str, Any]]],
    n: int = 3,
    ttl: Optional[str] = None,
    foundation_len: Optional[int] = None,
) -> Any:
    """Copy *messages* and stamp ``cache_control`` on the conversation breakpoints.

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
    out = [dict(m) if isinstance(m, dict) else m for m in messages]
    for i in targets:
        out[i]["content"] = _marked_content(out[i].get("content"), marker)
    return out
