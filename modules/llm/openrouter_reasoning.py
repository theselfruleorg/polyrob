"""OpenRouter reasoning budget — `OPENROUTER_REASONING_MAX_TOKENS` (057 WS-B follow-up).

Prod 2026-09-20: eight output cuts at the 8,192 cap in five hours were REASONING
— 7,981/8,192 and 8,192/8,192 reasoning tokens per OpenRouter's generation
records — on steps as small as a `read_file`. The model thought past its output
budget and never reached the tool call; each cut cost a ~$0.01 retry and 119–181 s
of wall clock, which is the p90 tail 057 set out to cut. The output cap bounds
the ANSWER; this bounds the THINKING that precedes it. OpenRouter accepts
`reasoning: {"max_tokens": N}` and DeepSeek honours it.

Unset (or anything that is not a positive integer) sends no `reasoning` block —
byte-identical request. The budget is clamped to 3/4 of the call's output cap so
it can never itself guarantee the cut it exists to prevent. Lives beside
`openrouter_routing.py` so `openrouter_client.py` stays under its size ceiling.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

ENV = "OPENROUTER_REASONING_MAX_TOKENS"
#: 2026-09-20 11:05Z, measured live: DeepInfra (the upstream OpenRouter picks for
#: deepseek/deepseek-v4.1-flash) IGNORES `reasoning.max_tokens` and `effort` —
#: a 300-token budget still produced 1,200 reasoning tokens — while
#: `reasoning.enabled=false` produced 0. On this seat the honoured lever is
#: on/off. Set to a falsey value ("false"/"0"/"off") to send `enabled: false`.
ENV_ENABLED = "OPENROUTER_REASONING_ENABLED"

#: the share of the output cap that reasoning may take at most
_MAX_SHARE = 0.75


def resolve_reasoning_max_tokens() -> Optional[int]:
    """The configured reasoning budget, or None when unset/invalid."""
    raw = (os.getenv(ENV) or "").strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


def reasoning_disabled() -> bool:
    """`OPENROUTER_REASONING_ENABLED` set to a falsey value — thinking OFF for this seat."""
    raw = (os.getenv(ENV_ENABLED) or "").strip().lower()
    return raw in {"0", "false", "off", "no", "none"}


def reasoning_extra_body(max_tokens: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The `extra_body` reasoning block for this call, or None when no knob is set.
    `enabled: false` wins over a budget (the upstream ignores budgets anyway)."""
    if reasoning_disabled():
        return {"reasoning": {"enabled": False}}
    budget = resolve_reasoning_max_tokens()
    if budget is None:
        return None
    if isinstance(max_tokens, int) and max_tokens > 0:
        budget = min(budget, int(max_tokens * _MAX_SHARE))
    if budget <= 0:
        return None
    return {"reasoning": {"max_tokens": budget}}


def apply_reasoning_budget(request_params: Dict[str, Any],
                           max_tokens: Optional[int] = None) -> Dict[str, Any]:
    """Merge the reasoning block into a chat.completions request (in place)."""
    body = reasoning_extra_body(max_tokens)
    if body:
        extra = dict(request_params.get("extra_body") or {})
        extra.update(body)
        request_params["extra_body"] = extra
    return request_params


def apply_request_extras(request_params: Dict[str, Any], client: Any = None,
                         max_tokens: Optional[int] = None) -> Dict[str, Any]:
    """The ONE call the client makes before `chat.completions.create`: provider
    routing (`OPENROUTER_PROVIDER_SORT` / the per-session reroute) then the
    reasoning budget, both merged into `extra_body`. Kept here so the client
    file stays under its size ceiling."""
    from modules.llm.openrouter_routing import apply_provider_routing
    from modules.llm.prefix_stamp import stamp_client
    apply_provider_routing(request_params, client)
    if client is not None:
        stamp_client(client, request_params)   # prefix identity for the billing record
    return apply_reasoning_budget(request_params, max_tokens)
