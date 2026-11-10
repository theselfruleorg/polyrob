"""Where `temperature` / `top_p` / `top_k` go.

The real API has none of them on current models: a request that sends one is a 400, and
anthropic 1.6 removed them from `messages.create()`. So they never go to the real API, through
any `base_url`. Only a named compat seat (`AnthropicCompatClient`, whose third-party endpoint
still reads them) sends them, in `extra_body`, because the SDK method does not take them.
"""
from __future__ import annotations

from typing import Any, Dict

SAMPLING_PARAMS = ("temperature", "top_p", "top_k")


def route_sampling(api_params: Dict[str, Any], *, compat_seat: bool) -> Dict[str, Any]:
    """A copy of *api_params* with the sampling params in their place: the tool path retries
    the same dict, so an edit in place would compound across attempts."""
    if not any(name in api_params for name in SAMPLING_PARAMS):
        return api_params
    out = dict(api_params)
    rescued = {name: out.pop(name) for name in SAMPLING_PARAMS if name in out}
    if compat_seat:
        # An explicit extra_body is the caller stating what goes on the wire, so it wins.
        out["extra_body"] = {**rescued, **(out.get("extra_body") or {})}
    return out


def context_room(client, messages, system, tools_tokens: int) -> int:
    """The output room the context window leaves this request, with the input counted as
    ``LLMClient._adjust_max_tokens`` counts it (the step's anchor, else the estimate)."""
    from modules.llm.call_scope import anchored_input_tokens
    used = anchored_input_tokens()
    if used is None:
        used = client._estimate_input_tokens(messages, system) + int(tools_tokens or 0)
    return client.get_context_window() - used - 100


#: The budget clamp of extended thinking, on the rows older than 4.6 that hold a budget.
def clamp_thinking(model_cap, budget, current_max_tokens, room=None):
    """Return (budget, max_tokens) valid for Anthropic extended thinking.

    Anthropic requires ``max_tokens > thinking.budget_tokens`` AND
    ``max_tokens <= the model's real completion cap``. Registry entries set
    budget == cap, so ``max_tokens = budget + 4096`` would overrun the cap and 400.
    Shrink the budget to leave >=4096 output room under the cap; if the cap is too
    small to fit any thinking, return (None, ...) so the caller disables thinking.
    ``room`` (the context window's room for output) caps it like the model cap does: a
    budget never lifts max_tokens past what the context window leaves.
    """
    if room is not None and room <= 0:
        # No output room is left: no thinking, and max_tokens is not lifted.
        return None, current_max_tokens
    if room is not None:
        model_cap = min(model_cap, room) if model_cap and model_cap > 0 else room
    if not budget or budget <= 0:
        return None, current_max_tokens
    if model_cap and model_cap > 0:
        max_budget = model_cap - 4096
        if max_budget < 1024:
            # Cap too small to fit thinking + output room — disable thinking.
            return None, min(current_max_tokens, model_cap)
        if budget > max_budget:
            budget = max_budget
        max_tokens = current_max_tokens
        if max_tokens <= budget:
            max_tokens = budget + 4096
        max_tokens = min(max_tokens, model_cap)
        return budget, max_tokens
    # No cap known — bump above budget.
    max_tokens = current_max_tokens
    if max_tokens <= budget:
        max_tokens = budget + 4096
    return budget, max_tokens
