"""112 W2: a model tier on a goal or a cron job — set only from a genuine owner turn.

The agent may name a tier (``core.runtime_config.ROUTABLE_TIERS``) with a one-line reason when
the owner's chat asked for the work. An autonomous run, a leaf or a forged turn cannot: unattended
spend never moves by a model's choice (D3). ``core.runtime_config.model_for`` reads the stored
``payload.tier`` at dispatch; the goal/cron runner records the route (``core.model_routing``).
"""
from typing import Optional, Tuple

import agents.task.constants  # noqa: F401 — 112: registers the light-tier resolver

TIER_FIELD_DESCRIPTION = (
    "Optional model tier for this work's runs — from the owner's chat only: 'heavy' for work "
    "that decides money (who is paid, how much, a winner) or is hard to undo; 'light' for bulk "
    "reads; 'default' = the normal model. <environment> names each tier's model. Requires 'why'.")
WHY_FIELD_DESCRIPTION = "One line: why this tier. Required with 'tier'; shown in /status."


def tier_payload(tier: Optional[str], why: Optional[str], owner_turn: bool,
                 provider: Optional[str] = None) -> Tuple[dict, Optional[str]]:
    """``(payload keys to write, refusal)``. No tier → ``({}, None)``. ``default`` writes an
    explicit default (and clears an earlier tier)."""
    if not tier:
        return {}, None
    if not owner_turn:
        return {}, ("Refused: a model tier on a goal or job needs a genuine owner turn — a run, "
                    "a sub-agent or a forged turn cannot choose the model unattended work uses.")
    from core.model_routing import clean_why
    if not clean_why(why):
        return {}, "Refused: 'tier' requires 'why' (one line: why this tier)."
    from core.runtime_config import resolve_default_provider, tier_refusal
    refusal = tier_refusal(tier, provider or resolve_default_provider()[0])
    if refusal:
        return {}, f"Cannot set tier: {refusal}"
    return {"tier": tier, "tier_why": clean_why(why)}, None
