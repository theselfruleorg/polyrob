"""Goal pre-flight: do not START work a due rail will pre-empt (057 WS-C B10).

056 WS5 (2b) asked for this and it was never built (tree grep: 0 hits). The
headroom rule (``GOAL_DISPATCH_CRON_HEADROOM_SEC``) bounds a goal's start by a
FIXED window; it knows nothing about how long THIS goal takes. Prod 2026-09-19
started a 14-step goal three minutes before the hourly rail and bought a
guaranteed yield — the goal lost its run, the rail waited anyway.

The estimate is the goal's OWN measured history: the p95 of
``duration_sec / steps`` over its prior ``goal_run`` terminal events, times its
step budget, clamped by ``GOAL_MAX_RUN_SECONDS``. With no history the flagged
fallback (``GOAL_PREFLIGHT_STEP_SEC``, default 45 s) is used and SAID to be a
fallback — an unmeasured goal is not a fast goal.

Every function here is READ-ONLY and fail-open: an unreadable telemetry db or
cron store must never stop dispatch, only stop this extra refusal.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Terminal goal_run outcomes that carry a real runtime.
_TERMINAL = ("done", "failed")

#: How far back the per-step history is read (seconds) and how many runs count.
_HISTORY_WINDOW_SEC = 14 * 24 * 3600
_HISTORY_MAX_RUNS = 20


def p95(values: List[float]) -> Optional[float]:
    """Nearest-rank p95 of *values*, or None for an empty list.

    Nearest-rank (not interpolated) on purpose: with 3 samples the honest p95 is
    the slowest one, and an interpolated figure would read as a measurement that
    was never measured."""
    vals = sorted(float(v) for v in values if v is not None and float(v) > 0)
    if not vals:
        return None
    idx = max(0, min(len(vals) - 1, int(round(0.95 * len(vals) + 0.5)) - 1))
    return vals[idx]


def measured_step_seconds(goal_id: str, *, data_dir: Optional[str] = None,
                          now_ts: Optional[float] = None) -> Optional[float]:
    """p95 seconds-per-step for THIS goal's prior runs, or None when unmeasured.

    Reads the durable event log; a run whose event carries no ``duration_sec``
    or no ``steps`` contributes nothing (it is not counted as fast)."""
    try:
        from core.event_log import open_event_log
        log = open_event_log(data_dir)
        if log is None:
            return None
        since = (now_ts if now_ts is not None else time.time()) - _HISTORY_WINDOW_SEC
        rows = log.query(kind="goal_run", since_ts=since, limit=2000)
    except Exception:
        logger.debug("preflight history read skipped", exc_info=True)
        return None
    per_step: List[float] = []
    for r in rows:
        attrs: Dict[str, Any] = r.get("attrs") or {}
        if attrs.get("goal_id") != goal_id:
            continue
        if attrs.get("outcome") not in _TERMINAL:
            continue
        try:
            dur = float(attrs.get("duration_sec") or 0)
            steps = int(attrs.get("steps") or 0)
        except (TypeError, ValueError):
            continue
        if dur <= 0 or steps <= 0:
            continue
        per_step.append(dur / steps)
        if len(per_step) >= _HISTORY_MAX_RUNS:
            break
    return p95(per_step)


def expected_run_seconds(goal_id: str, max_steps: int, *,
                         fallback_step_sec: int, max_run_sec: int,
                         data_dir: Optional[str] = None) -> Tuple[float, bool]:
    """``(seconds, measured)`` this goal is expected to need.

    ``measured`` is False when the fallback step time was used, so a caller can
    say which it is instead of presenting a guess as a measurement."""
    step = measured_step_seconds(goal_id, data_dir=data_dir)
    measured = step is not None
    if step is None:
        step = float(max(1, int(fallback_step_sec)))
    est = float(max(1, int(max_steps))) * step
    if max_run_sec > 0:
        est = min(est, float(max_run_sec))
    return est, measured


def next_preempting_rail(data_dir: str, now: datetime) -> Optional[Tuple[str, datetime]]:
    """``(job_id, next_run_at)`` of the soonest job that may PRE-EMPT, else None.

    Read-only over ``<data_dir>/cron.db`` via ``core.sqlite_util`` (the layering
    ratchet forbids agents -> cron), mirroring ``dispatcher.imminent_cron_job``.
    An absent or unreadable store answers None: fail-open, dispatch."""
    db_path = os.path.join(data_dir, "cron.db")
    if not os.path.exists(db_path):
        return None
    try:
        from core.sqlite_util import execute_retry
        rows = execute_retry(
            db_path,
            "SELECT id, next_run_at, payload FROM cron_jobs WHERE enabled=1 "
            "AND status='scheduled' AND next_run_at IS NOT NULL "
            "ORDER BY next_run_at", (), fetch="all")
    except Exception:
        logger.debug("preflight rail probe failed; dispatching", exc_info=True)
        return None
    from agents.task.goals.dispatcher import _payload_preempts
    for job_id, next_run_at, payload in rows or []:
        if not _payload_preempts(payload):
            continue
        try:
            when = datetime.fromisoformat(str(next_run_at))
        except (TypeError, ValueError):
            continue
        return (str(job_id), when)
    return None


def crossing_rail(goal_id: str, max_steps: int, *, data_dir: str, now: datetime,
                  fallback_step_sec: int, max_run_sec: int) -> Optional[str]:
    """The job id this goal would run INTO, or None.

    None also means "cannot tell" — an unreadable store, an unparseable time —
    because a pre-flight that refuses on ignorance stops the board."""
    try:
        rail = next_preempting_rail(data_dir, now)
        if rail is None:
            return None
        job_id, when = rail
        est, _measured = expected_run_seconds(
            goal_id, max_steps, fallback_step_sec=fallback_step_sec,
            max_run_sec=max_run_sec, data_dir=data_dir)
        headroom = (when - now).total_seconds()
        return job_id if est > headroom else None
    except Exception:
        logger.debug("preflight check skipped for %s", goal_id, exc_info=True)
        return None
