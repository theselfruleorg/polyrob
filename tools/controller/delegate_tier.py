"""112 W2: `delegate_task(tier=…, why=…)` — the agent names a tier, the harness picks the model.

The tier set is the operator's (``core.runtime_config.tier_models``: default / heavy / light on
the operator's pinned provider). This module turns each tier into the model id the existing
build path takes (``_build_child_llm`` → ``child_model_refusal``, which still bounds it), caps
heavy children per call, and records every choice (``core.model_routing``). It lives here because
``action_registration.py`` is at its size ceiling.
"""
from dataclasses import dataclass, field
from typing import Any, List, Optional

import agents.task.constants  # noqa: F401 — 112: registers the light-tier resolver

#: D12: at most this many heavy sub-agents per delegate_task call.
MAX_HEAVY_PER_CALL = 2


@dataclass
class TierRoute:
    model: Optional[str] = None
    provider: Optional[str] = None
    tasks: Optional[List[Any]] = None
    error: str = ""
    notes: List[str] = field(default_factory=list)
    records: List[dict] = field(default_factory=list)

    def commit_records(self) -> None:
        """Record every routed child (``core.model_routing``), once. Call it only when
        the children will run: a refused call or a failed build ran nothing."""
        from core.model_routing import record_model_route
        pending, self.records = self.records, []
        for rec in pending:
            record_model_route(**rec)

    @property
    def head(self) -> str:
        """The result-head suffix that names the model(s) the tiers ran on."""
        return f" — {'; '.join(self.notes)}" if self.notes else ""


def _parent_runtime(parent_agent):
    provider = (getattr(parent_agent, "llm_provider", None)
                or getattr(parent_agent, "provider_name", None) or "")
    return provider, getattr(parent_agent, "model_name", None)


def _resolve(tier: str, parent_agent, *, env=None):
    """``(model or None = inherit, provider, refusal)`` for one tier."""
    from core.runtime_config import tier_models, tier_refusal, operator_provider_pin
    provider, own = _parent_runtime(parent_agent)
    provider = provider or operator_provider_pin(env) or ""
    refusal = tier_refusal(tier, provider, env)
    if refusal:
        return None, None, refusal
    if tier == "default":
        return None, None, ""
    model = tier_models(provider, env).get(tier)
    if not model or model == own:
        return None, None, ""          # the tier IS the parent's model: inherit it
    return model, provider, ""


def resolve_delegate_tiers(*, parent_agent, model, provider, tier, why, tasks,
                           session_id: str = "", user_id: str = "", env=None,
                           defer_record: bool = False) -> TierRoute:
    """Turn ``tier`` (top level) and each task's ``tier`` into models. A raw ``model`` passes
    through untouched (the owner-alias path, D4). Any refusal refuses the call, naming the
    task — the same rule as a bad raw model (no silent partial run), and records nothing.
    With ``defer_record`` the caller calls ``route.commit_records()`` after the child
    models are built; otherwise the routes are recorded when the whole call resolves."""
    route = _resolve_delegate_tiers(parent_agent=parent_agent, model=model, provider=provider,
                                    tier=tier, why=why, tasks=tasks, session_id=session_id,
                                    user_id=user_id, env=env)
    if route.error:
        route.records = []
    elif not defer_record:
        route.commit_records()
    return route


def _resolve_delegate_tiers(*, parent_agent, model, provider, tier, why, tasks,
                            session_id, user_id, env) -> TierRoute:
    route = TierRoute(model=model, provider=provider, tasks=tasks)
    if tasks:
        from core.runtime_config import heavy_model_pin
        pin = heavy_model_pin(env)
        # D12: a raw ``model`` equal to HEAVY_MODEL is a heavy child too (112 revalidation).
        heavy = sum(1 for st in tasks
                    if (getattr(st, "tier", None) or (tier if not (st.model or model) else None))
                    == "heavy" or (pin and (st.model or model) == pin))
        if heavy > MAX_HEAVY_PER_CALL:
            route.error = (f"refused: at most {MAX_HEAVY_PER_CALL} heavy sub-agents per call "
                           f"({heavy} asked) — run the rest on default or in a later call")
            return route
    if not tier and not any(getattr(t, "tier", None) for t in (tasks or [])):
        return route
    if parent_agent is None:
        route.error = "tier: the parent agent is unavailable, so no tier can be resolved"
        return route
    own = _parent_runtime(parent_agent)[1]
    if model and any(getattr(t, "tier", None) for t in (tasks or [])):
        # A task tier that resolves to "inherit" would run on the top-level raw model
        # (``st.model or model``) and the record would name the wrong one (112 review).
        route.error = ("refused: a task 'tier' with a top-level 'model' — set the model per "
                       "task, or use tiers only")
        return route

    def _one(t, w, label):
        m, p, refusal = _resolve(t, parent_agent, env=env)
        if refusal:
            route.error = f"{label}: {refusal}"
            return None, None
        route.records.append(dict(kind="child", tier=t, model=m or own, why=w,
                                  source="delegate_task", user_id=user_id, session_id=session_id))
        route.notes.append(f"{label} ran on {m or own} ({t})")
        return m, p

    if tasks:
        out = []
        for i, st in enumerate(tasks):
            t = getattr(st, "tier", None) or (tier if not st.model else None)
            if not t:
                out.append(st)
                continue
            m, p = _one(t, getattr(st, "why", None) or why, f"task[{i}]")
            if route.error:
                return route
            out.append(st.model_copy(update={"model": m, "provider": p if m else None,
                                             "tier": None}))
        route.tasks = out
        if tier:                        # the top level is the fallback, already applied
            route.model, route.provider = None, None
        return route
    m, p = _one(tier, why, "goal")
    route.model, route.provider = m, p
    return route
