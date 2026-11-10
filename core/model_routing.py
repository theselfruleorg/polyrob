"""112 W2: the ONE record of a model tier the agent chose — and why.

The agent names a tier (``core.runtime_config.ROUTABLE_TIERS``) with a one-line reason; the
harness resolves it inside the operator's bounds and records the route here. ``/status``
(the ``work`` section) shows the last choices; the run's result line names the model.
"""
from typing import Optional

from core.event_kinds import MODEL_ROUTED

WHY_MAX = 200


def clean_why(why: Optional[str]) -> str:
    """The reason as it is stored and shown: one line, secret shapes scrubbed, capped."""
    from core.secret_scrub import scrub_secret_shapes
    return scrub_secret_shapes(" ".join(str(why or "").split()))[:WHY_MAX]


def record_model_route(*, kind: str, tier: str, model: Optional[str], why: Optional[str],
                       source: str, user_id: str = "", session_id: str = "") -> None:
    """Fail-open: one ``model_routed`` event. *kind* is ``child``, ``goal`` or ``job``."""
    from core.event_log import emit
    emit(MODEL_ROUTED, source=source, user_id=user_id, session_id=session_id,
         attrs={"kind": kind, "tier": tier, "model": model or "", "why": clean_why(why)})


def record_stored_tier(*, kind: str, payload, model: Optional[str], absent: bool,
                       user_id: str = "", ref: str = "") -> None:
    """A goal/job run with a stored ``payload.tier``: record the model it runs on. *absent* =
    the tier no longer resolves (the operator changed the env) — the run uses the default
    model, and the record says so (never a silent fallback)."""
    tier = str((payload or {}).get("tier") or "")
    if not tier:
        return
    if absent:
        import logging
        logging.getLogger(__name__).warning(
            "%s %s: stored tier %r does not resolve now — running on %s", kind, ref, tier, model)
        record_model_route(kind=kind, tier="default", model=model,
                           why=f"tier {tier} absent", source=f"{kind}_run", user_id=user_id)
        return
    why = (payload or {}).get("tier_why")
    if tier != "heavy":
        from core.money.classify import is_money_class
        if is_money_class(payload):     # money class wins over a stored light/default tier
            tier, why = "heavy", f"money-class (stored tier {tier}); {why or ''}".strip("; ")
    if tier == "heavy":
        # The record names the tier the run REALLY got: no HEAVY_MODEL, or a provider that
        # is not the operator's pin, runs the money work on the default model.
        from core.runtime_config import heavy_model_pin
        heavy = heavy_model_pin()
        if not heavy or (model and model != heavy):
            tier, why = "default", f"heavy not configured for this run; {why or ''}".strip("; ")
    record_model_route(kind=kind, tier=tier, model=model, why=why,
                       source=f"{kind}_run", user_id=user_id)
