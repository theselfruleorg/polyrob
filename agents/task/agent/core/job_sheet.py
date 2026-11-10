"""The job's half of the ``<previous-runs>`` block (108 W3).

A cron run used to read log files for what the stores already hold. Each part here
reads one store and prints one short section: the job's acts, its open asks, its money
and its sibling jobs. ``episodic_digest.render_previous_runs`` adds the outcomes and
owns the total cap.

Three answers stay distinct: a row, ``none``, and ``could not read`` (an unreadable
store is not an empty one). A part prints stored times only: the block must not change
because the clock moved.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

ACTS_WINDOW_DAYS = 7
_ACTS_ROWS = 200
_ACTS_CHARS, _ASKS_CHARS, _SIBLING_CHARS = 340, 150, 200
_ASKS_KEPT, _SIBLINGS_KEPT = 3, 4
_ASK_TITLE_CHARS, _SIBLING_TASK_CHARS = 60, 30
_SUBJECT_CHARS, _REF_CHARS = 32, 60
_UNREADABLE = "could not read"
#: A part answers its text and whether it holds a fact (``none`` does not).
Part = Tuple[str, bool]


def _line(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _stamp(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%MZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _iso_stamp(value: Any) -> str:
    """A stored ``next_run_at``, in the form of every other time in the block."""
    if not value:
        return "not set"
    try:
        return _stamp(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        return _line(value, 20)


def _fit(head: str, lines: List[str], limit: int, unit: str = "more") -> str:
    """``head`` and the lines that fit in ``limit``; the rest is a count, never silent."""
    out, used = [head], len(head)
    for i, ln in enumerate(lines):
        rest = len(lines) - i - 1
        tail = len(f"\n- (+{rest} {unit})") if rest else 0
        if used + len(ln) + 1 + tail > limit:
            out.append(f"- (+{len(lines) - i} {unit})")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def _acts(log: Any, job_id: str, users: set, since_ts: float) -> Part:
    from core.effects import GATED_EFFECTS
    from core.event_kinds import EXTERNAL_WRITE
    head = f"acts in {ACTS_WINDOW_DAYS} days:"
    rows = log.rows_where(kind=EXTERNAL_WRITE, effects=sorted(GATED_EFFECTS), since_ts=since_ts,
                          user_ids=users, attrs_in={"cron_job_id": job_id},
                          limit=_ACTS_ROWS) if log else []
    if rows is None:
        return f"{head} {_UNREADABLE}", True
    if not rows:
        return f"{head} none", False
    verbs: dict = {}
    for r in rows:
        verbs.setdefault(str(r["attrs"].get("action") or "?"), []).append(r)
    lines = []
    for verb, acts in verbs.items():
        lines.append(f"- {verb} x{len(acts)}{'+' if len(rows) >= _ACTS_ROWS else ''}:")
        for r in acts:
            a = r["attrs"]
            bits = [_stamp(r["ts"])]
            if a.get("subject"):
                bits.append(f"subject={_line(a['subject'], _SUBJECT_CHARS)}")
            if a.get("ref"):
                bits.append(f"ref={_line(a['ref'], _REF_CHARS)}")
            if a.get("outcome") not in (None, "ok"):
                bits.append(f"outcome={a['outcome']}")
            lines.append("  - " + " ".join(bits))
    return _fit(head, lines, _ACTS_CHARS), True


def _asks(job_id: str, user_id: Optional[str], data_dir: Optional[str]) -> Part:
    from core.runtime_paths import goals_db_path
    head = "open asks:"
    db = goals_db_path(data_dir)
    if not os.path.exists(db):
        return f"{head} none", False
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.rail_answers import open_rail_asks
    asks = open_rail_asks(GoalBoard(db), str(user_id or ""), f"cron:{job_id}")
    if not asks:
        return f"{head} none", False
    lines = [f"- {_stamp(a.created_at)} {_line(a.title, _ASK_TITLE_CHARS)}"
             for a in asks[:_ASKS_KEPT]]
    if len(asks) > _ASKS_KEPT:
        lines.append(f"- (+{len(asks) - _ASKS_KEPT} more)")
    return _fit(head, lines, _ASKS_CHARS), True


def _money(log: Any, job_id: str, users: set, since_ts: float) -> Part:
    from core.event_kinds import TX_SETTLED, WALLET_SPEND
    head = f"money in {ACTS_WINDOW_DAYS} days:"
    rollup = log.job_money(job_id=job_id, since_ts=since_ts, user_ids=users) if log else {}
    if rollup is None:
        return f"{head} {_UNREADABLE}", True
    if not rollup:
        return f"{head} none", False
    spend = rollup.get(WALLET_SPEND) or {"n": 0, "usd": 0.0, "unpriced": 0}
    unpriced = f", {spend['unpriced']} unpriced" if spend["unpriced"] else ""
    settled = (rollup.get(TX_SETTLED) or {}).get("n", 0)
    return f"{head} spends {spend['n']} (${spend['usd']:.2f}{unpriced}), settled {settled}", True


def _siblings(job_id: str, user_id: Optional[str], data_dir: Optional[str]) -> Part:
    from core.cron_lanes import job_lane
    from core.runtime_paths import cron_db_path
    from core.sqlite_util import execute_retry
    db = cron_db_path(data_dir)
    if not os.path.exists(db):
        return "other jobs: none", False
    rows = execute_retry(
        db, "SELECT id, task, status, next_run_at, payload FROM cron_jobs "
            "WHERE user_id=? AND enabled=1 AND status NOT IN ('cancelled','done','failed') "
            "ORDER BY next_run_at IS NULL, next_run_at, id",
        (str(user_id or ""),), fetch="all") or []

    def lane_of(payload: Any) -> str:
        try:
            return job_lane(json.loads(payload or "{}"))
        except ValueError:
            return ""

    own = [r for r in rows if r[0] == job_id]
    lane = lane_of(own[0][4]) if own else ""
    others = [r for r in rows if r[0] != job_id and (not lane or lane_of(r[4]) == lane)]
    head = f"other jobs in lane {lane}:" if lane else "other jobs:"
    if not others:
        return f"{head} none", False
    lines = []
    for r in others[:_SIBLINGS_KEPT]:
        status = "" if r[2] == "scheduled" else f" ({r[2]})"
        lines.append(f'- {r[0]}{status} next {_iso_stamp(r[3])} "{_line(r[1], _SIBLING_TASK_CHARS)}"')
    if len(others) > _SIBLINGS_KEPT:
        lines.append(f"- (+{len(others) - _SIBLINGS_KEPT} more)")
    return _fit(head, lines, _SIBLING_CHARS), True


def _part(head: str, read: Callable[[], Part]) -> Part:
    try:
        return read()
    except Exception:
        logger.warning("job sheet: %s unreadable", head, exc_info=True)
        return f"{head}: {_UNREADABLE}", True


def job_sections(*, job_id: str, user_id: Optional[str],
                 data_dir: Optional[str] = None) -> Tuple[List[str], bool]:
    """``(sections, any_fact)`` for one job. Never raises. ``any_fact`` is False when every
    store answered "none", so a first run with nothing to say renders no block."""
    from core.event_log import open_event_log
    since_ts = time.time() - ACTS_WINDOW_DAYS * 86400
    users = {str(user_id or ""), ""}
    log, log_error = None, False
    try:
        log = open_event_log(data_dir)
    except Exception:
        logger.warning("job sheet: event log did not open", exc_info=True)
        log_error = True

    def from_log(fn: Callable[..., Part]) -> Callable[[], Part]:
        def read() -> Part:
            if log_error:
                raise RuntimeError("event log did not open")
            return fn(log, job_id, users, since_ts)
        return read

    parts = [_part("acts", from_log(_acts)),
             _part("open asks", lambda: _asks(job_id, user_id, data_dir)),
             _part("money", from_log(_money)),
             _part("other jobs", lambda: _siblings(job_id, user_id, data_dir))]
    return [text for text, _ in parts], any(fact for _, fact in parts)
