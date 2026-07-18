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

_TERMINAL = {"done", "failed", "partial", "cancelled", None}   # None allowed for chat close


async def finalize_episode(*, session_id: str, user_id: Optional[str], kind: str,
                           task: Optional[str] = None, outcome: Optional[str] = None,
                           summary: Optional[str] = None,
                           artifacts: Optional[List[Dict[str, Any]]] = None,
                           spend_usd: float = 0.0, steps: int = 0,
                           goal_id: Optional[str] = None, thread_key: Optional[str] = None,
                           started_ts: Optional[int] = None,
                           meta: Optional[Dict[str, Any]] = None) -> None:
    try:
        from core.config_policy import AutonomyConfig
        if not AutonomyConfig.episodic_memory_enabled():
            return
        if outcome not in _TERMINAL:                # don't stamp an in-progress row
            return
        from modules.memory.provider import EpisodeRecord
        from modules.memory.registry import memory_record_episode
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
      (the same per-session cost the run-loop's own cost-summary log line uses),
      read as ``total_user_cost_usd``.
    - steps: sum of ``agent.state.n_steps`` across the orchestrator's non-sub-agent
      agents (``orchestrator.agents``, skipping ``agent._is_sub_agent``).
    - artifacts: the evidence pack's bounded, no-LLM collector (B4/D4 — ledger
      descriptors for real-output actions + time-windowed workspace file scan,
      ``agents.task.runtime.evidence.collect_artifacts``). ``[]`` on any error.

    Every lookup is defensive: a ``None`` orchestrator, a missing attribute, a
    ``None`` usage_tracker, or a DB error all degrade silently to the zero value.
    This function NEVER raises and NEVER blocks a run/reset completion.
    """
    out: Dict[str, Any] = {"spend_usd": 0.0, "steps": 0, "artifacts": []}
    if orchestrator is None:
        return out
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
            out["spend_usd"] = float(breakdown.get("total_user_cost_usd", 0.0) or 0.0)
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
