"""121: a model the agent names — checked against the one price rule (``core.model_choice``).

``child_model_check`` checks ``delegate_task(model=…)``: the agent may name ANY model of a
provider that has a key; the run spend cap bounds the money. ``models_view`` is what the agent reads before
it chooses (``agent_status(view="models")``).
"""
import logging
from typing import Any, List, Optional, Tuple

from core import model_choice as mc, model_switch as ms

logger = logging.getLogger(__name__)

#: The models read shows at most this many models beyond the owner's own.
MENU_MAX = 20


def _canon(provider) -> str:
    from core.runtime_config import canonicalize_provider
    return (canonicalize_provider(provider) or str(provider or "")).lower() if provider else ""


def owner_models(user_id: Optional[str]) -> dict:
    """``{model id: label}`` for every model the owner or the operator chose: the main,
    heavy and light model, the owner's chat default and each owner alias."""
    from core import model_pins
    from core.runtime_config import operator_provider_pin, tier_models
    from agents.task.model_switch_service import _known_providers
    out = {}
    tiers = tier_models(operator_provider_pin())
    for tier, label in (("default", "main"), ("heavy", "heavy"), ("light", "light")):
        if tiers.get(tier):
            out.setdefault(tiers[tier], label)
    pin = model_pins.chat_pin_for(user_id)
    if pin and pin.get("model"):
        out.setdefault(pin["model"], "your default")
    known = set(_known_providers())
    for name, target in model_pins.aliases(user_id).items():
        head, _, rest = target.partition("/")
        out.setdefault(rest if head in known and rest else target, f"alias {name}")
    return out


def is_listed(user_id: Optional[str], provider: Optional[str], model: str,
              own: Tuple[Optional[str], Optional[str]] = (None, None)) -> bool:
    """A model the owner uses or listed: *own* (the caller's own model), the main / heavy /
    light model, the owner's chat default or an alias. An owner-chosen id counts on the
    owner's providers only: the same id on another provider is another price and another
    account (112 F3). An empty *provider* = the caller's own provider."""
    own_provider, own_model = own
    if not (model and (model == own_model or model in owner_models(user_id))):
        return False
    if not provider:
        return True
    from core.runtime_config import operator_provider_pin
    mine = {_canon(p) for p in (own_provider, operator_provider_pin()) if p}
    return not mine or _canon(provider) in mine


async def child_model_check(parent_agent: Any, model: str, provider: Optional[str],
                            route: Any = None) -> str:
    """Why ``delegate_task`` may not run a sub-agent on *model* (``""`` = it may). On
    success the model and its price are added to *route*'s notes, so the result names what
    the sub-agent really ran on."""
    from agents.task.model_switch_service import _keyed_providers, _model_facts
    own = getattr(parent_agent, "model_name", None)
    owner = getattr(parent_agent, "user_id", None)
    own_provider = (getattr(parent_agent, "llm_provider", None)
                    or getattr(parent_agent, "provider_name", "") or "")
    target_provider = _canon(provider) or _canon(own_provider)
    keyed = {_canon(p) for p in _keyed_providers()}
    if provider and keyed and target_provider not in keyed:
        return (f"refused: no key for provider '{provider}'. Providers with a key: "
                f"{', '.join(sorted(keyed))}.")
    listed = is_listed(owner, target_provider if provider else "", model,
                       own=(own_provider, own))
    facts = await _model_facts(target_provider, model)
    theirs = facts[0]
    if not listed and len(facts) > 5 and not facts[5]:
        return f"refused: '{model}' is not a model of {target_provider or 'the provider'}."
    if not listed and theirs and len(facts) > 3 and facts[3] is False:
        return f"refused: '{model}' cannot call tools, and a sub-agent works through tools."
    verdict, why = mc.decide(listed=listed, mode=mc.choice_mode(owner), new_price=theirs)
    if verdict != mc.ALLOW:
        return (f"refused: '{model}' — {why}. agent_status view='models' lists the models you "
                f"can use and their prices.")
    notes = getattr(route, "notes", None)
    if notes is not None:
        notes.append(f"{model}: {ms.fmt_price(theirs)}" + (f" ({why})" if why else ""))
    return ""


async def models_view(user_id: Optional[str], current: Tuple[str, str]) -> str:
    """The models the agent may name, for ``agent_status(view="models")``: the owner's own
    first, then the other priced models of the keyed providers, cheapest first, with the
    price of *current* ``(provider, model)`` to compare."""
    import asyncio
    from agents.task.model_switch_service import _keyed_providers, _model_facts
    cur_provider, cur_model = current
    cur_price = (await _model_facts(_canon(cur_provider), cur_model or ""))[0]
    mode = mc.choice_mode(user_id)
    lines = [f"You run on {cur_model or '?'} ({cur_provider or '?'}): {ms.fmt_price(cur_price)}.",
             "Name a model with model= and a one-line why= (delegate_task, goal_create, "
             "cronjob_schedule). Any model of a provider with a key runs: compare the prices "
             "and choose; the run spend cap bounds the cost. A model with no known price runs "
             "with a warning."]
    if mode == mc.CHOICE_LISTED:
        lines.append("The owner allows ONLY the listed models (model choice: listed).")
    lines.append("Listed (the owner uses them — always allowed):")
    mine = owner_models(user_id)
    for model, label in mine.items():
        price = (await _model_facts(_canon(cur_provider), model))[0]
        lines.append(f"- {model} ({label}): {ms.fmt_price(price)}")
    if not mine:
        lines.append("- none beyond your own model")
    if mode == mc.CHOICE_LISTED:
        return "\n".join(lines)
    rows: List[tuple] = []
    unread: List[str] = []
    for provider in _keyed_providers():
        try:
            rows += await asyncio.to_thread(_provider_models, _canon(provider))
        except Exception as exc:  # an unreadable list is named, never shown as empty
            unread.append(f"{provider} ({type(exc).__name__})")
    rows = [r for r in rows if r[1] not in mine and r[1] != cur_model]
    rows.sort(key=lambda r: (r[2][1], r[2][0], r[1]))
    lines.append(f"Other models with a known price (cheapest first, {min(len(rows), MENU_MAX)} "
                 f"of {len(rows)}):")
    for provider, model, price, context in rows[:MENU_MAX]:
        lines.append(f"- {provider}/{model}: {ms.fmt_price(price)}"
                     + (f", context {context:,}" if context else ""))
    if unread:
        lines.append("Could not read the model list of: " + ", ".join(unread) + ".")
    return "\n".join(lines)


def _provider_models(provider: str) -> List[tuple]:
    """``[(provider, model, (in, out), context)]`` — every priced, tool-calling model."""
    out: List[tuple] = []
    if provider == "openrouter":
        from modules.llm import openrouter_catalog as oc
        table = oc.catalog()
        if table is None:
            raise RuntimeError("the OpenRouter model list could not be read")
        for model, entry in table.items():
            if entry.price and entry.tools and entry.price[1] > 0:
                out.append((provider, model, entry.price, entry.context))
        return out
    from modules.llm.model_registry import get_all_models
    for cfg in get_all_models():
        if _canon(getattr(cfg.provider, "value", cfg.provider)) != provider:
            continue
        pr = cfg.pricing
        if pr and pr.output_price and cfg.capabilities.supports_function_calling:
            out.append((provider, cfg.name, (pr.input_price, pr.output_price),
                        cfg.context_window))
    return out
