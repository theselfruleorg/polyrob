"""112 W2 / 121: the model of a goal or a cron job — a tier or a model, from any turn.

The agent names a tier (``core.runtime_config.ROUTABLE_TIERS``) or ANY model (``model=…``),
from an owner turn or from an unattended run. The bound of a model is the one price rule
(``core.model_choice``); a tier is a model the owner or the operator already chose. A model
the OWNER set is locked: the agent's tools change neither it nor its tier.
``core.runtime_config.model_for`` reads the stored ``payload.tier`` at dispatch, a stored
``payload.model`` wins, and the goal/cron runner records the route (``core.model_routing``).
"""
from typing import Optional, Tuple

import agents.task.constants  # noqa: F401 — 112: registers the light-tier resolver

TIER_FIELD_DESCRIPTION = (
    "Optional model tier for this work's runs: 'heavy' for work that decides money (who is "
    "paid and how much) or is hard to undo; 'light' for bulk reads; 'default' = the normal "
    "model. <environment> names each tier's model (the owner or the operator sets it). "
    "Requires 'why'.")
WHY_FIELD_DESCRIPTION = ("One line: why this tier or model. Required with 'tier'; shown in "
                         "/status.")

_MODEL_KEYS = ("model", "model_by", "model_why")


def _owner_locked(existing: Optional[dict], owner_turn: bool) -> Optional[str]:
    """The refusal when the agent would change a model the OWNER set (None = it may)."""
    existing = existing if isinstance(existing, dict) else {}
    # A stored model with no ``model_by`` is an owner pin from before 121 (the owner CLI).
    if existing.get("model") and existing.get("model_by") != "agent" and not owner_turn:
        return (f"Refused: the owner set the model of this work ({existing['model']}); "
                "only the owner changes it.")
    return None


def tier_payload(tier: Optional[str], why: Optional[str], owner_turn: bool,
                 provider: Optional[str] = None,
                 existing: Optional[dict] = None) -> Tuple[dict, Optional[str]]:
    """``(payload keys to write, refusal)``. No tier → ``({}, None)``. ``default`` writes an
    explicit default (and clears an earlier tier). A tier replaces a stored model (*existing*
    is the stored payload of the row being changed), except a model the owner set."""
    if not tier:
        return {}, None
    locked = _owner_locked(existing, owner_turn)
    if locked:
        return {}, locked
    from core.model_routing import clean_why
    if not clean_why(why):
        return {}, "Refused: 'tier' requires 'why' (one line: why this tier)."
    from core.runtime_config import resolve_default_provider, tier_refusal
    keys = {"tier": tier, "tier_why": clean_why(why)}
    if isinstance(existing, dict) and existing.get("model"):
        keys.update({k: None for k in _MODEL_KEYS})   # the run reads payload.model first
        keys["provider"], provider = None, None       # the model brought its provider
    refusal = tier_refusal(tier, provider or resolve_default_provider()[0])
    if refusal:
        return {}, f"Cannot set tier: {refusal}"
    return keys, None


MODEL_FIELD_DESCRIPTION = (
    "Optional model for this work's runs: any model id or alias of a provider that has a key "
    "(agent_status view='models' lists them with prices); 'default' clears it. A model with "
    "no known price runs with a warning. Give a one-line 'why'. Not with 'tier'. A model the owner set is locked.")

_CLEAR_WORDS = ("default", "none", "-")


async def model_payload(model: Optional[str], why: Optional[str], *, tier: Optional[str],
                        owner_turn: bool, user_id: Optional[str], existing: Optional[dict] = None,
                        what: str = "") -> Tuple[dict, Optional[str], str, str]:
    """``(payload keys to write, refusal, result note, owner notice)`` for a model the agent
    names on a goal or a job. No model → ``({}, None, "", "")``. *existing* is the stored
    payload of the row being changed (None on a create). ``model_by`` records who set it:
    ``owner`` on a genuine owner turn (and from the owner CLI), else ``agent``. The notice
    is non-empty when an UNATTENDED run changes a stored model, or creates work on a model
    the owner does not use or list: the caller sends it with :func:`notify_model_change`
    AFTER the write succeeded."""
    name = str(model or "").strip()
    if not name:
        return {}, None, "", ""
    if tier:
        return {}, "Refused: use 'tier' or 'model', not both.", "", ""
    locked = _owner_locked(existing, owner_turn)
    if locked:
        return {}, locked, "", ""
    existing = existing if isinstance(existing, dict) else {}
    from core.model_routing import clean_why
    reason = clean_why(why)
    if name.lower() in _CLEAR_WORDS:
        if not existing.get("model"):
            return {}, None, "", ""
        updates = {k: None for k in _MODEL_KEYS}
        updates["provider"] = None         # the model brought its provider with it
        notice = "" if owner_turn else (
            f"I set the model of {what or 'a job'} back to the default"
            + (f": {reason}" if reason else "") + ".")
        return updates, None, " Model: back to the default.", notice
    from agents.task import model_switch_service as svc
    from agents.task.model_choice import is_listed, price_ceiling
    from core import model_choice as mc, model_switch as ms
    from core.runtime_config import resolve_default_provider
    words = name.split()
    if len(words) > 2 or (len(words) == 2 and "/" in words[0]):
        return {}, f"Refused: '{name}' is not a model name.", "", ""
    cmd = ms.ModelCommand(action="set", name=words[-1], words=words,
                          provider=words[0].lower() if len(words) == 2 else "")
    plan, err = await svc.plan_switch(cmd, user_id=str(user_id or ""))
    if plan is None:
        return {}, "Model " + err.replace("❌ ", "").replace(" Nothing changed.", ""), "", ""
    main_provider, main_model = resolve_default_provider()[:2]
    if not main_model:
        try:
            from modules.llm.llm_client_registry import get_default_model
            main_model = get_default_model(main_provider)
        except Exception:
            main_model = None
    listed = is_listed(user_id, plan.provider, plan.model, own=(main_provider, main_model))
    verdict, reason_why = mc.decide(
        listed=listed, mode=mc.choice_mode(user_id), new_price=plan.price,
        ceiling=None if plan.price else price_ceiling(plan.provider))
    if verdict != mc.ALLOW:
        return {}, (f"Refused: '{plan.model}' — {reason_why}. agent_status view='models' "
                    f"lists the models you can use and their prices."), "", ""
    by = "owner" if owner_turn else "agent"
    updates = {"model": plan.model, "provider": plan.provider, "model_by": by,
               "model_why": reason, "tier": None, "tier_why": None}
    note = (f" Model: {plan.model} ({plan.provider}), {ms.fmt_price(plan.price)}; set by "
            f"{'the owner turn' if owner_turn else 'the agent'}"
            + (f" — {reason}" if reason else "") + "."
            + (f" {reason_why.capitalize()}." if reason_why else ""))
    notice = ""
    if not owner_turn and (existing.get("model") != plan.model if existing else not listed):
        # A change of a stored model, or new work on a model the owner does not use or list.
        head = (f"I changed the model of {what or 'a job'} to" if existing else
                f"I created {what or 'a job'} on a model you do not use or list:")
        notice = (f"{head} {plan.model} ({ms.fmt_price(plan.price)})"
                  + (f": {reason}" if reason else "")
                  + ". To set it yourself: polyrob cron edit --model / polyrob goals edit "
                  "--model.")
    return updates, None, note, notice


def notify_model_change(controller, text: str) -> None:
    """One owner notice when an UNATTENDED run changed the model of standing work or created
    work on a model off the owner's set. Call it
    after the write succeeded; an empty *text* sends nothing."""
    if not text:
        return
    try:
        from core.async_bridge import spawn_retained
        from core.self_evolution import push_owner_message
        spawn_retained(push_owner_message(getattr(controller, "container", None), text,
                                          priority="low", source="model_choice"))
    except Exception:
        import logging
        logging.getLogger(__name__).warning("model change notice not delivered: %s", text)
