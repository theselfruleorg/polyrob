"""Fail-open episodic-write facade + since-parser (2026-07-03).

The ONLY surface the three completion sites (goal dispatcher, cron runner, chat
teardown/reset) touch. Keeps callers dumb: flag gate + tenant guard + EpisodeRecord
build + no-throw. A memory error here must NEVER block a run/reset completion.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# "blocked" / "skipped" (2026-10-13 review C-11): a run that ended by declaring
# BLOCKED or SKIPPED is not a plain success; the run record says which.
_TERMINAL = {"done", "failed", "partial", "cancelled", "blocked", "skipped",
             None}   # None allowed for chat close


async def finalize_episode(*, session_id: str, user_id: Optional[str], kind: str,
                           task: Optional[str] = None, outcome: Optional[str] = None,
                           summary: Optional[str] = None,
                           artifacts: Optional[List[Dict[str, Any]]] = None,
                           spend_usd: float = 0.0, steps: int = 0,
                           goal_id: Optional[str] = None, thread_key: Optional[str] = None,
                           started_ts: Optional[int] = None,
                           meta: Optional[Dict[str, Any]] = None,
                           skills_loaded: Optional[List[str]] = None,
                           skills_activation: Optional[Dict[str, Any]] = None,
                           run_record: bool = False) -> None:
    """``skills_loaded`` (080 WS-5): the skill ids this run had active, stamped
    onto ``meta["skills_loaded"]`` so "which skill did last night's job use?"
    has a per-run answer (``skill_usage`` is only a lifetime count).

    ``skills_activation`` (081 WS-4.6, :func:`skills_activation`): how they got
    there. ``meta["skills_auto"]`` = the skills the session PINNED at start (a
    job's or goal's pins, persona seeds, their ``requires``); every other id in
    ``skills_loaded`` the model chose with ``load_skill`` (explicit).
    ``meta["skills_regime"]`` = the session regime, so the auto/explicit split
    reads per regime. Measurement only: nothing retires a skill from it.

    ``run_record`` (2026-10-13 review R3): the row is a cron job's or a goal's
    RUN RECORD — the next run of the same job/goal reads it as its
    ``<previous-runs>`` block (``thread_key`` = ``cron:<id>``/``goal:<id>``).
    The caller decides it from the regime seam
    (``agents.task.session_class.regime_policy(...).run_record``). A run record
    is the job's own continuity, so it is written even when the posture leaves
    ``EPISODIC_MEMORY_ENABLED`` off; the memory backend still decides whether
    any episode store exists."""
    try:
        from core.config_policy import AutonomyConfig
        if not (run_record or AutonomyConfig.episodic_memory_enabled()):
            return
        if outcome not in _TERMINAL:                # don't stamp an in-progress row
            return
        from modules.memory.provider import EpisodeRecord
        from modules.memory.registry import memory_record_episode
        if skills_loaded:
            meta = {**(meta or {}), "skills_loaded": sorted({str(s) for s in skills_loaded})}
        act = skills_activation if isinstance(skills_activation, dict) else {}
        if act.get("auto"):
            meta = {**(meta or {}), "skills_auto": sorted({str(s) for s in act["auto"]})}
        if act.get("regime"):
            meta = {**(meta or {}), "skills_regime": str(act["regime"])}
        rec = EpisodeRecord(
            ts=int(time.time()), started_ts=started_ts, user_id=user_id or "",
            session_id=session_id, thread_key=thread_key, kind=kind, task=task,
            outcome=outcome, summary=summary, artifacts=artifacts or [],
            spend_usd=float(spend_usd or 0), steps=int(steps or 0), goal_id=goal_id,
            meta=meta,
        )
        await memory_record_episode(rec, session_id=session_id, user_id=user_id)
    except Exception:
        logger.warning("finalize_episode failed", exc_info=True)   # NEVER block completion


async def collect_provenance(orchestrator: Any) -> Dict[str, Any]:
    """Best-effort ``{spend_usd, steps, artifacts}`` for a finished run.

    Takes the LIVE resident orchestrator object (e.g. ``task_agent.get_orchestrator
    (session_id)``, called immediately after ``run_session`` returns, while it is
    still resident) — NOT a bare session_id, since neither ``SessionManager``
    metadata nor the goal/cron board carry cost or step totals.

    - spend_usd: ``orchestrator.usage_tracker.get_session_breakdown(session_id)``
      read as ``total_api_cost_usd`` — the provider's real charge, the same
      figure ``core/status_economics.py`` renders. NOT ``total_user_cost_usd``:
      that is the platform BILLING figure (credits × $0.01, every call rounded
      UP to a whole credit), ~4× the API cost on the owner's own seat, and until
      2026-09-21 it fed the rail ledger, the episodic digest, goal-event
      provenance and the owner digest as an unlabelled "spend". On a single-
      owner deploy the owner IS the payer. The billed figure is carried
      separately as ``billed_usd`` for the seats platform billing needs.
    - steps: sum of ``agent.state.n_steps`` across the orchestrator's non-sub-agent
      agents (``orchestrator.agents``, skipping ``agent._is_sub_agent``).
    - artifacts: the evidence pack's bounded, no-LLM collector (B4/D4 — ledger
      descriptors for real-output actions + time-windowed workspace file scan,
      ``agents.task.runtime.evidence.collect_artifacts``). ``[]`` on any error.

    Every lookup is defensive: a ``None`` orchestrator, a missing attribute, a
    ``None`` usage_tracker, or a DB error all degrade silently to the zero value.
    This function NEVER raises and NEVER blocks a run/reset completion.
    """
    out: Dict[str, Any] = {"spend_usd": 0.0, "steps": 0, "artifacts": [],
                           "skills_loaded": [], "skills_activation": {}}
    if orchestrator is None:
        return out
    out["skills_loaded"] = skills_loaded(orchestrator)
    out["skills_activation"] = skills_activation(orchestrator)
    try:
        steps = 0
        for agent in list((getattr(orchestrator, "agents", None) or {}).values()):
            if getattr(agent, "_is_sub_agent", False):
                continue
            steps += int(getattr(getattr(agent, "state", None), "n_steps", 0) or 0)
        out["steps"] = steps
    except Exception:
        pass
    try:
        tracker = getattr(orchestrator, "usage_tracker", None)
        session_id = getattr(orchestrator, "session_id", None)
        if tracker is not None and session_id:
            breakdown = await tracker.get_session_breakdown(session_id)
            out["spend_usd"] = float(breakdown.get("total_api_cost_usd", 0.0) or 0.0)
            out["billed_usd"] = float(breakdown.get("total_user_cost_usd", 0.0) or 0.0)
    except Exception:
        pass
    try:
        # Lazy import (same pattern as finalize_episode's constants import) so this
        # module keeps working when the agents package isn't importable.
        from agents.task.runtime import evidence as _evidence
        arts = _evidence.collect_artifacts(orchestrator)
        if arts:
            out["artifacts"] = arts
    except Exception:
        pass
    return out


def skills_loaded(orchestrator: Any) -> List[str]:
    """The skill ids a run had ACTIVE (eager-pinned or ``load_skill``-ed): the
    session controller's ``_activated_skills`` set, minus the repeat-ack
    markers. ``[]`` on any error — never raises."""
    ids: set = set()
    try:
        ctls = [getattr(orchestrator, "controller", None)]
        for agent in list((getattr(orchestrator, "agents", None) or {}).values()):
            if not getattr(agent, "_is_sub_agent", False):
                ctls.append(getattr(agent, "controller", None))
        for ctl in ctls:
            act = getattr(ctl, "_activated_skills", None)
            if isinstance(act, (set, frozenset, list, tuple)):
                ids.update(str(s) for s in act if s and "#" not in str(s))
    except Exception:
        return []
    return sorted(ids)


def skills_activation(orchestrator: Any) -> Dict[str, Any]:
    """081 WS-4.6: ``{"auto": [ids], "regime": str}`` — the skills the session
    pinned at start (``controller._auto_skills``, set at construction) and its
    regime (``controller._skill_regime``). ``{}`` on any error — never raises."""
    auto: set = set()
    regime = ""
    try:
        ctls = [getattr(orchestrator, "controller", None)]
        for agent in list((getattr(orchestrator, "agents", None) or {}).values()):
            if not getattr(agent, "_is_sub_agent", False):
                ctls.append(getattr(agent, "controller", None))
        for ctl in ctls:
            pins = getattr(ctl, "_auto_skills", None)
            if isinstance(pins, (set, frozenset, list, tuple)):
                auto.update(str(s) for s in pins if s)
            regime = regime or str(getattr(ctl, "_skill_regime", "") or "")
    except Exception:
        return {}
    out: Dict[str, Any] = {}
    if auto:
        out["auto"] = sorted(auto)
    if regime:
        out["regime"] = regime
    return out


def parse_since(text: Optional[str]) -> Optional[int]:
    """Parse '8h'/'2d'/'30m'/'45s'/ISO8601 into a unix epoch lower-bound; None if unusable."""
    if not text:
        return None
    text = text.strip()
    m = re.fullmatch(r"(\d+)\s*([smhd])", text.lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        secs = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
        return int(time.time()) - secs
    try:
        from datetime import datetime, timezone
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None
