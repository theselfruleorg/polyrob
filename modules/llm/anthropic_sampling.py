"""Where `temperature` / `top_p` / `top_k` go, per installed SDK and endpoint.

⚠️ **2026-09-24.** anthropic 1.6.0 removed the sampling params from
`messages.create()` (sampling moved to `output_config.effort`). Every param
builder in `anthropic_client` puts `temperature` in the top-level kwargs, so the
first call on ANY Anthropic-protocol seat died with::

    TypeError: AsyncMessages.create() got an unexpected keyword argument 'temperature'

It was latent from the 09-16 move to the OpenRouter seat until the credit
sentinel latched OpenRouter and the fallback reached `zai-coding`.

Two things make the obvious fixes wrong:

* **Just dropping it** silently re-samples every turn on the third-party
  Anthropic-COMPATIBLE endpoints (z.ai and friends), which still honour the
  field on the wire. A quiet behaviour change is worse than a loud crash.
* **Always smuggling it through `extra_body`** sends a field the canonical API
  has removed, which is a 400 on every call rather than a default.

So the rule is: ask the function we are about to call what it accepts, and send
what it cannot take through `extra_body` only when we are NOT talking to
api.anthropic.com. Asking the callable, rather than pinning a version, is what
keeps this correct across the next SDK upgrade in either direction.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, FrozenSet, Optional

#: The params that moved. `stop_sequences` did NOT — it is still a real kwarg.
SAMPLING_PARAMS = ("temperature", "top_p", "top_k")

#: The one endpoint known to have removed them. Everything else — z.ai, a
#: self-hosted gateway, a proxy — is a compatible endpoint until proven
#: otherwise, and compatible endpoints still read the field.
CANONICAL_HOST = "api.anthropic.com"


def accepted_params(create_fn: Callable[..., Any]) -> FrozenSet[str]:
    """The keyword names *create_fn* will accept, or an empty set if unknowable.

    Unknown means "do not pass the risky kwarg": a callable we cannot inspect is
    the case where a TypeError would be a crash in the hot path, so it fails
    toward the route that always works.
    """
    try:
        params = inspect.signature(create_fn).parameters
    except (TypeError, ValueError):
        return frozenset()
    kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    return frozenset(name for name, p in params.items() if p.kind in kinds)


def accepts_any_kwarg(create_fn: Callable[..., Any]) -> bool:
    """True when *create_fn* has a ``**kwargs``, so no keyword can TypeError.

    ⚠️ This is what keeps the fix from rewriting the whole test tree: the SDK's
    real `create` is generated with explicit keyword-only params and NO
    ``**kwargs`` (checked against anthropic 1.6.0), while most stubs in this repo
    are `async def create(self, **params)`. A stub that swallows everything is
    not a stub that rejects `temperature`, and pretending otherwise would make
    every mock assert a behaviour the real client does not have.
    """
    try:
        params = inspect.signature(create_fn).parameters
    except (TypeError, ValueError):
        return False
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _is_canonical(base_url: Optional[str]) -> bool:
    # No base_url means the SDK default, which IS the canonical API.
    if not base_url:
        return True
    return CANONICAL_HOST in str(base_url).lower()


def apply_sampling_params(api_params: Dict[str, Any], *,
                          create_fn: Callable[..., Any],
                          base_url: Optional[str]) -> Dict[str, Any]:
    """Return a copy of *api_params* the installed SDK can actually be called with.

    A copy, not an in-place edit: the tool path RETRIES the same dict (the
    deferred-tools self-heal), so mutating it would compound across attempts.
    """
    if accepts_any_kwarg(create_fn):
        return api_params
    accepted = accepted_params(create_fn)
    movable = [name for name in SAMPLING_PARAMS
               if name in api_params and name not in accepted]
    if not movable:
        return api_params

    out = dict(api_params)
    rescued = {name: out.pop(name) for name in movable}
    if _is_canonical(base_url):
        return out

    # An explicit extra_body is the caller stating what goes on the wire, so it
    # wins over the kwarg we are rescuing.
    extra_body = dict(out.get("extra_body") or {})
    for name, value in rescued.items():
        extra_body.setdefault(name, value)
    out["extra_body"] = extra_body
    return out


def route_for_client(client: Any, api_params: Dict[str, Any]) -> Dict[str, Any]:
    """Route *api_params* for the bound ``client.messages.create`` (the client's seam).

    Fails open: a client that is not initialised yet (``None``) or a stub/mock
    without resources passes the params through unchanged, so this routing step
    is never the thing that raises.
    """
    if client is None:
        return api_params
    try:
        create_fn = client.messages.create
    except Exception:      # a stub/mock client in tests has no resources
        return api_params
    return apply_sampling_params(
        api_params, create_fn=create_fn,
        base_url=str(getattr(client, "base_url", "") or "") or None)


#: Extended thinking pins ``temperature`` to 1, so its budget clamp lives beside
#: the sampling rules (moved out of anthropic_client for the size ratchet).
def clamp_thinking(model_cap, budget, current_max_tokens):
    """H4: return (budget, max_tokens) valid for Anthropic extended thinking.

    Anthropic requires ``max_tokens > thinking.budget_tokens`` AND
    ``max_tokens <= the model's real completion cap``. Registry entries set
    budget == cap, so the old ``max_tokens = budget + 4096`` overran the cap and 400'd.
    Shrink the budget to leave >=4096 output room under the cap; if the cap is too
    small to fit any thinking, return (None, ...) so the caller disables thinking.
    """
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
    # No cap known — preserve legacy behaviour (bump above budget).
    max_tokens = current_max_tokens
    if max_tokens <= budget:
        max_tokens = budget + 4096
    return budget, max_tokens
