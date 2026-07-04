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
    for var in ("LLM_PROMPT_CACHE", "ANTHROPIC_PROMPT_CACHE"):
        if os.getenv(var, "1").lower() in ("0", "false", "no", "off"):
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
    if p in ("anthropic", "openai"):
        return "in_client"
    if p in ("deepseek", "nvidia"):
        return "automatic"     # disk/KV cache; NIM reuse is operator-side
    if p == "openrouter":
        return openrouter_cache_strategy(model)
    if p == "gemini":
        return "explicit"      # implicit is free + needs no code; explicit is opt-in
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
    passthrough is not explicitly enabled (``OPENROUTER_PROMPT_CACHE``, default off —
    the request-format change should be live-verified per gateway before defaulting on).

    Returns the (possibly mutated) message list; never raises.
    """
    if not prompt_cache_enabled():
        return formatted_messages
    if not _bool_env("OPENROUTER_PROMPT_CACHE", False):
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
    if not _bool_env("OPENROUTER_PROMPT_CACHE", False):
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
