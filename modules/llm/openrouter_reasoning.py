"""OpenRouter reasoning budget — `OPENROUTER_REASONING_MAX_TOKENS`.

Output cuts at the 8,192 cap were REASONING
— 7,981/8,192 and 8,192/8,192 reasoning tokens per OpenRouter's generation
records — on steps as small as a `read_file`. The model thought past its output
budget and never reached the tool call; each cut cost a ~$0.01 retry and 119–181 s
of wall clock, which is the p90 tail. The output cap bounds
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

from modules.llm.call_scope import session_reasoning_level
from modules.llm.catalog_lookup import catalog_row, catalog_wire_id

ENV = "OPENROUTER_REASONING_MAX_TOKENS"
#: Measured live: DeepInfra (the upstream OpenRouter picks for
#: deepseek/deepseek-v4.1-flash) IGNORES `reasoning.max_tokens` and `effort` —
#: a 300-token budget still produced 1,200 reasoning tokens — while
#: `reasoning.enabled=false` produced 0. On this seat the honoured lever is
#: on/off. Set to a falsey value ("false"/"0"/"off") to send `enabled: false`.
ENV_ENABLED = "OPENROUTER_REASONING_ENABLED"

#: the share of the output cap that reasoning may take at most
_MAX_SHARE = 0.75

#: Models that answered "Reasoning is mandatory" in this process. The catalog row of a known
#: one says so itself (``capabilities.reasoning_mandatory``).
_LEARNED: set = set()


def _canonical(model: Optional[str]) -> str:
    return (catalog_wire_id((model or "").strip(), "openrouter") or "").lower()


def reasoning_mandatory(model: Optional[str]) -> bool:
    """True when *model* cannot have reasoning switched off. An alias reads its row."""
    m = _canonical(model)
    row = catalog_row(m)
    return bool(m) and (m in _LEARNED or bool(row and row.capabilities.reasoning_mandatory))


def remember_reasoning_mandatory(model: Optional[str]) -> None:
    """Remember (for the process) a model that answered "Reasoning is mandatory"."""
    m = _canonical(model)
    if m:
        _LEARNED.add(m)


def is_reasoning_mandatory_rejection(exc: BaseException) -> bool:
    """The upstream 400 for ``enabled:false`` on a model that needs reasoning."""
    return "reasoning is mandatory" in str(exc).lower()


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


def reasoning_block(level: Optional[str]) -> Optional[Dict[str, Any]]:
    """The OpenRouter ``reasoning`` object for a level (``core/model_switch.py``
    validates it): off/on → ``enabled``, low/medium/high → ``effort``, digits →
    ``max_tokens``. None = send nothing."""
    if not level:
        return None
    if level == "off":
        return {"enabled": False}
    if level == "on":
        return {"enabled": True}
    if level in ("low", "medium", "high"):
        return {"effort": level}
    if str(level).isdigit():
        return {"max_tokens": int(level)}
    return None


def _session_block(max_tokens: Optional[int]) -> Optional[Dict[str, Any]]:
    level = session_reasoning_level()
    if not level:
        return None
    block = reasoning_block(level)
    if block and "max_tokens" in block and isinstance(max_tokens, int) and max_tokens > 0:
        block["max_tokens"] = max(1, min(block["max_tokens"], int(max_tokens * _MAX_SHARE)))
    return {"reasoning": block} if block else None


def reasoning_extra_body(max_tokens: Optional[int] = None,
                         model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The `extra_body` reasoning block for this call, or None when no knob is set.
    The owner's session level wins over the env; `enabled: false` wins over a
    budget (the upstream ignores budgets anyway). A model whose reasoning is
    MANDATORY never gets `enabled: false` (it would 400): its "off" falls through
    to the budget, or to no block at all."""
    mandatory = reasoning_mandatory(model)
    own = _session_block(max_tokens)
    if own and not (mandatory and own["reasoning"].get("enabled") is False):
        return own
    if reasoning_disabled() and not mandatory:
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
    body = reasoning_extra_body(max_tokens, request_params.get("model"))
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
    file stays under its size ceiling. A subclass that is NOT OpenRouter (DeepSeek)
    supplies its own ``request_extras`` and none of these blocks are sent."""
    own = getattr(client, "request_extras", None)
    if callable(own):
        return own(request_params, max_tokens)
    from modules.llm.openrouter_routing import apply_provider_routing, apply_session_id
    from modules.llm.prefix_stamp import stamp_request
    apply_provider_routing(request_params)
    if getattr(client, "SENDS_SESSION_ID", False):
        apply_session_id(request_params)
    stamp_request(request_params)   # request identity for the billing record
    apply_usage_accounting(request_params)
    return apply_reasoning_budget(request_params, max_tokens)


def apply_usage_accounting(request_params: Dict[str, Any]) -> Dict[str, Any]:
    """Ask OpenRouter for the cost it ACTUALLY billed.

    Every cost figure in the tree is an ESTIMATE recomputed from the model
    catalog's per-token prices. On OpenRouter that estimate is a guess about a
    router: the upstream it picked, its own discounts and its cache pricing are
    not in our table, and a `cache_discount` never appears in it at all. With
    `usage: {"include": true}` the response's `usage` block carries `cost`
    (USD, what OpenRouter charged) and `cache_discount`, which
    `modules/llm/usage_extract.py` reads and the billing chokepoint stores as
    the real `api_cost_usd`.

    Free on every OpenRouter model (it only asks for accounting in the
    response) and merged into `extra_body` beside the routing and reasoning
    blocks. In place; never raises.
    """
    extra = dict(request_params.get("extra_body") or {})
    usage = dict(extra.get("usage") or {})
    usage["include"] = True
    extra["usage"] = usage
    request_params["extra_body"] = extra
    return request_params


async def create_with_reasoning_fallback(client: Any, sdk: Any, request_params: Dict[str, Any],
                                         create: Any = None) -> Any:
    """One ``chat.completions.create`` that survives a model whose reasoning is MANDATORY.

    ``create`` is the call to make (default: the SDK's own; the tools path passes
    ``create_with_tool_choice_fallback``). When the upstream answers 400 "Reasoning is
    mandatory", the model is remembered for the process, the ``enabled:false`` block is
    swapped for the configured budget (or dropped), and the call is retried ONCE.
    Any other error propagates unchanged."""
    async def _call() -> Any:
        if create is not None:
            return await create(client, sdk, request_params)
        return await sdk.chat.completions.create(**request_params)
    try:
        return await _call()
    except Exception as e:
        if not is_reasoning_mandatory_rejection(e):
            raise
        model = request_params.get("model") or getattr(client, "model_type", None)
        remember_reasoning_mandatory(model)
        extra = dict(request_params.get("extra_body") or {})
        if (extra.get("reasoning") or {}).get("enabled") is False:
            extra.pop("reasoning", None)
            request_params["extra_body"] = extra
            apply_reasoning_budget(request_params, request_params.get("max_tokens"))
        logger = getattr(client, "logger", None)
        if logger is not None:
            logger.warning(f"event=reasoning_mandatory model={model} "
                           f"action=retry_without_off ({str(e)[:160]})")
        return await _call()
