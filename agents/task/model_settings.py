"""121 phase 3: the owner's model settings on a chat seat — ``/model heavy|light <name>``,
``/model choice free|listed`` — and the agent's last model choices for the ``/model`` view.

A setting is owner state (``model.json``); it moves no live chat. ``heavy`` and ``light`` are
read by ``core.runtime_config`` (``heavy_model_pin`` / ``_light_model``) for the instance
owner, on the operator's provider; ``choice`` is read by ``core.model_choice``.
"""
import logging
from typing import List, Optional

from core import model_choice as mc, model_pins, model_switch as ms

logger = logging.getLogger(__name__)

CHOICE_WORDS = {
    mc.CHOICE_FREE: "free — I may name any model, and I compare the prices",
    mc.CHOICE_LISTED: "listed — I may name only the models you use or listed (/model alias)",
}


def _owner_only(user_id: Optional[str]) -> str:
    from core.instance import is_owner_uid
    return "" if is_owner_uid(user_id) else "❌ Only the owner sets this. Nothing changed."


def _owner_tenant(user_id: Optional[str]) -> str:
    """The tenant whose ``model.json`` the settings live in: the bound owner principal (the
    one ``core.runtime_config`` and the agent read), else *user_id* (the local operator)."""
    from core.instance import resolve_owner_principal
    return resolve_owner_principal() or str(user_id or "")


async def settings_command(cmd: ms.ModelCommand, user_id: str, seat: str = "") -> str:
    """Run a ``choice`` or a ``heavy`` / ``light`` line. Returns the reply text."""
    user_id = _owner_tenant(user_id) if not _owner_only(user_id) else user_id
    if cmd.action == "choice":
        if not cmd.name:
            return "Model choice: " + CHOICE_WORDS[mc.choice_mode(user_id)] + "."
        refused = _owner_only(user_id)
        if refused:
            return refused
        ok, err = model_pins.set_choice(user_id, cmd.name)
        return (f"✅ Model choice: {CHOICE_WORDS[cmd.name]}." if ok
                else f"❌ Not saved ({err}). Nothing changed.")
    tier = cmd.tier
    refused = _owner_only(user_id)
    if refused:
        return refused
    if cmd.action == "reset":
        ok, err = model_pins.set_tier(user_id, tier, None, None)
        if not ok:
            return f"❌ Not saved ({err}). Nothing changed."
        from core.runtime_config import tier_models, operator_provider_pin
        now = tier_models(operator_provider_pin()).get(tier)
        return f"✅ {tier}: back to the operator's value ({now or 'not set: it uses the main model'})."
    if cmd.action != "set":
        return f"Usage: /model {tier} <name> | /model {tier} reset"
    from agents.task import model_switch_service as svc
    from core.runtime_config import canonicalize_provider, operator_model_pin, operator_provider_pin
    pinned = operator_provider_pin()
    current = (pinned, operator_model_pin()) if tier == "heavy" else (None, None)
    plan, err = await svc.plan_switch(cmd, user_id=user_id, current=current)
    if plan is None:
        return err
    if pinned and (canonicalize_provider(plan.provider) or plan.provider) != (
            canonicalize_provider(pinned) or pinned):
        return (f"❌ The {tier} model is on the operator's provider ({pinned}); "
                f"{plan.model} is on {plan.provider}. Nothing changed.")
    ok, err = model_pins.set_tier(user_id, tier, plan.provider, plan.model)
    if not ok:
        return f"❌ Not saved ({err}). Nothing changed."
    try:
        from core.event_log import emit
        emit("model_switched", source="owner_setting", user_id=str(user_id or ""),
             attrs={"tier": tier, "to": f"{plan.provider}/{plan.model}", "seat": seat})
    except Exception:
        logger.debug("model_switched event skipped", exc_info=True)
    use = ("work that decides money, and the `heavy` tier" if tier == "heavy"
           else "the `light` tier: bulk reads and summaries")
    return (f"✅ {tier}: {plan.model} ({plan.provider}), {ms.fmt_price(plan.price, plan.cached)}.\n"
            f"It serves {use}, from the next run. A run in progress keeps its model.")


def settings_lines(user_id: Optional[str]) -> List[str]:
    """The owner's settings and the agent's last choices, for the bare ``/model`` view."""
    from core.runtime_config import operator_provider_pin, tier_models
    tiers = tier_models(operator_provider_pin())
    user_id = _owner_tenant(user_id) if not _owner_only(user_id) else user_id
    lines = []
    unreadable = model_pins.load(user_id)[1] if user_id else ""
    if unreadable:
        lines.append(f"⚠️ Your model settings could not be read ({unreadable}); the "
                     "operator's values apply and I may choose only the listed models.")
    for tier in ("heavy", "light"):
        own = model_pins.owner_tier(user_id, tier)
        model = tiers.get(tier)
        lines.append(f"{tier.capitalize()} model: {model or 'not set (it uses the main model)'}"
                     + (" — set by you" if own and own.get("model") == model else
                        (" — operator env" if model else "")))
    lines.append("What I may choose for a helper, a goal or a job: "
                 + CHOICE_WORDS[mc.choice_mode(user_id)] + ".")
    lines += recent_choices(user_id)
    lines.append("Set: /model heavy <name> · /model light <name> · /model choice free|listed")
    return lines


def recent_choices(user_id: Optional[str], n: int = 5) -> List[str]:
    """The last *n* models somebody named for a helper, a goal or a job, newest first.
    An unreadable event log is named, never shown as "no choices"."""
    try:
        from core.event_kinds import MODEL_ROUTED
        from core.event_log import get_event_log
        rows = get_event_log().query(kind=MODEL_ROUTED, user_id=str(user_id or ""),
                                     limit=200, strict=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("model choices not read", exc_info=True)
        return [f"My last model choices could not be read ({type(exc).__name__})."]
    out = []
    for row in rows:
        a = row.get("attrs") or {}
        if a.get("tier") in ("default", "", None):
            continue
        what = {"child": "helper"}.get(a.get("kind"), a.get("kind") or "?")
        out.append(f"  {what}: {a.get('model') or '?'}"
                   + (f" ({a['tier']})" if a.get("tier") != "model" else "")
                   + (f" — {a['why']}" if a.get("why") else ""))
        if len(out) >= n:
            break
    return (["My last model choices (newest first):"] + out) if out else []
