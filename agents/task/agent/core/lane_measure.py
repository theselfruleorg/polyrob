"""What a reviewer's live change did to its lane (108 W6).

Each live change stores its lane and a before window of steps, errors, cost and corrections.
The row is a ``skill_usage`` state key, not a new store. The curator compares the runs after the
change with that window, with no LLM. Harm rolls the change back once and gives the owner one line.

This is a weak test at low volume: it catches only a large harm. A lane under
:data:`MIN_LANE_RUNS` runs reads ``unknown``, and its error rate decides nothing.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from core.state.machine import StateMachine

logger = logging.getLogger(__name__)

#: One change, one result: every state after ``open`` is final, so a rollback runs once.
CHANGE_MEASURE = StateMachine(
    "skill_change_measure",
    states={"open", "kept", "unknown", "superseded", "rolled_back", "rollback_failed"},
    transitions={"open": {"kept", "unknown", "superseded", "rolled_back", "rollback_failed"}},
    terminal={"kept", "unknown", "superseded", "rolled_back", "rollback_failed"},
    initial={"open"},
)

KEY_PREFIX = "curator:measure:"
#: The comparison is due after this many lane runs, or after this many days.
MIN_LANE_RUNS = 10
WINDOW_DAYS = 14
_DAY_S = 86400.0
#: The before window: the newest runs of the lane in the WINDOW_DAYS before the change.
BEFORE_RUNS = 20
#: "An error rate that doubles" from a rate near zero is one error: below this rate, the floor
#: counts as the before rate.
ERROR_RATE_FLOOR = 0.1


def run_lane(row: Any) -> str:
    """The lane of a run record; a rail with no lane is its own lane."""
    return str((getattr(row, "meta", None) or {}).get("lane") or getattr(row, "thread_key", "") or "")


def run_errored(row: Any) -> bool:
    from agents.task.agent.core.background_review import run_error
    meta = getattr(row, "meta", None)
    return run_error(meta if isinstance(meta, dict) else {}, getattr(row, "outcome", ""))


def window(rows: Iterable[Any]) -> Dict[str, Any]:
    """Pure: the totals of a set of run records."""
    rows = list(rows)
    return {
        "runs": len(rows),
        "steps": sum(int(getattr(r, "steps", 0) or 0) for r in rows),
        "errors": sum(1 for r in rows if run_errored(r)),
        "cost": round(sum(float(getattr(r, "spend_usd", 0.0) or 0.0) for r in rows), 6),
        "corrections": sum(1 for r in rows
                           if (getattr(r, "meta", None) or {}).get("owner_verdict") == "correct"),
    }


def _lane_rows(rows: Iterable[Any], lane: str) -> List[Any]:
    return [r for r in rows or [] if run_lane(r) == lane]


def before_window(rows: Iterable[Any], lane: str, change_ts: float) -> Dict[str, Any]:
    picked = sorted((r for r in _lane_rows(rows, lane) if int(r.ts) < change_ts),
                    key=lambda r: r.ts, reverse=True)[:BEFORE_RUNS]
    return window(picked)


def after_rows(rows: Iterable[Any], lane: str, change_ts: float) -> List[Any]:
    return [r for r in _lane_rows(rows, lane) if int(r.ts) >= change_ts]


def _rate(w: Dict[str, Any]) -> float:
    return w["errors"] / w["runs"] if w.get("runs") else 0.0


def error_rate_doubled(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    return bool(before.get("runs")) and _rate(after) >= 2 * max(_rate(before), ERROR_RATE_FLOOR)


def judge(row: Dict[str, Any], after: Dict[str, Any], now: float) -> Optional[str]:
    """Pure: ``harm_correction`` | ``harm_errors`` | ``unknown`` | ``kept``, or ``None`` when
    the comparison is not due. A new correction needs no sample: it is the owner's word."""
    if after["corrections"] >= 1:
        return "harm_correction"
    if after["runs"] < MIN_LANE_RUNS and now - float(row["ts"]) < WINDOW_DAYS * _DAY_S:
        return None
    before = row.get("before")
    if after["runs"] < MIN_LANE_RUNS or not (before or {}).get("runs"):
        return "unknown"
    return "harm_errors" if error_rate_doubled(before, after) else "kept"


def _per_run(w: Dict[str, Any], key: str) -> float:
    return w[key] / w["runs"] if w.get("runs") else 0.0


def finding(row: Dict[str, Any], after: Dict[str, Any], result: str) -> Dict[str, Any]:
    """The input of ``curator.contribution_line`` for one compared change."""
    return {"measure": result, "skill_id": row["skill_id"], "lane": row["lane"],
            "before": row.get("before") or {}, "after": after}


def measure_line(f: Dict[str, Any]) -> str:
    before, after = f["before"], f["after"]
    if f["measure"] == "unknown":
        return (f"{f['skill_id']}: the reviewer's change on lane {f['lane']} reads unknown: "
                f"{after['runs']} lane run(s) since the change, {before.get('runs', 0)} before; "
                f"a test needs {MIN_LANE_RUNS} on each side")
    return (f"{f['skill_id']}: the reviewer's change on lane {f['lane']}, per run after vs before: "
            f"errors {after['errors']}/{after['runs']} vs {before['errors']}/{before['runs']}, "
            f"steps {_per_run(after, 'steps'):.1f} vs {_per_run(before, 'steps'):.1f}, "
            f"cost ${_per_run(after, 'cost'):.3f} vs ${_per_run(before, 'cost'):.3f}, "
            f"corrections {after['corrections']} vs {before['corrections']}")


def rollback_line(row: Dict[str, Any], after: Dict[str, Any], result: str, ok: bool) -> str:
    why = (f"{after['corrections']} new owner correction(s)" if result == "harm_correction"
           else f"the error rate doubled ({after['errors']}/{after['runs']} runs)")
    if not ok:
        return (f"{row['skill_id']}: harm on lane {row['lane']} ({why}), and the automatic "
                f"rollback failed: the change is still live")
    return (f"{row['skill_id']}: I rolled back the reviewer's change on lane {row['lane']} ({why}); "
            f"the archive keeps it ({str(row.get('after_sha') or '')[:12]})")


def row_key(user_id: str, skill_id: str, after_sha: str) -> str:
    return f"{KEY_PREFIX}{user_id}/{skill_id}/{after_sha}"


async def record_changes(usage: Any, *, user_id: str, since_ts: float, rail_id: str,
                         signal: str, meta: Dict[str, Any]) -> int:
    """Store one open row per live change of the reviewer since *since_ts*. An unreadable run
    store leaves ``before`` empty, so the row reads ``unknown``: that is not a zero window. Returns
    the number of rows. Never raises."""
    try:
        changes = usage.live_changes_since(str(user_id), since_ts, actor="background_review")
    except Exception:
        logger.warning("lane measure: the skill ledger is unreadable, no measure row", exc_info=True)
        return 0
    if not changes:
        return 0
    lane = str(meta.get("lane") or rail_id)
    first_ts = min(float(c["ts"]) for c in changes)
    try:
        rows = await _read_runs(str(user_id), int(first_ts - WINDOW_DAYS * _DAY_S))
    except Exception:
        logger.warning("lane measure: run records unreadable, no before window", exc_info=True)
        rows = None
    done = 0
    for c in changes:
        after_sha = str(c.get("after_sha") or "")
        if not after_sha:
            continue
        row = {"state": "open", "user_id": str(user_id), "skill_id": str(c["skill_id"]),
               "lane": lane, "rail": rail_id, "signal": signal, "ts": float(c["ts"]),
               "before_sha": c.get("before_sha") or "", "after_sha": after_sha,
               "before": None if rows is None else before_window(rows, lane, float(c["ts"]))}
        try:
            usage.set_state(row_key(str(user_id), row["skill_id"], after_sha), json.dumps(row))
            done += 1
        except Exception:
            logger.warning("lane measure: row not stored for %s", row["skill_id"], exc_info=True)
    return done


async def _read_runs(user_id: str, since_ts: int) -> List[Any]:
    from modules.memory.registry import memory_recall_episodes
    out: List[Any] = []
    for kind in ("cron", "goal"):
        out += await memory_recall_episodes(user_id=user_id, since_ts=since_ts, kind=kind,
                                            limit=500, order="newest", raise_errors=True)
    return out


def open_rows(usage: Any) -> Dict[str, Dict[str, Any]]:
    """``{key: row}`` of the open rows. Raises when the store is unreadable."""
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in usage.list_state(KEY_PREFIX).items():
        try:
            row = json.loads(value)
        except (TypeError, ValueError):
            logger.warning("lane measure: bad row %s", key)
            continue
        if isinstance(row, dict) and row.get("state") == "open":
            out[key] = row
    return out


def close_row(usage: Any, key: str, row: Dict[str, Any], state: str, **extra: Any) -> None:
    """Move a row to a final state through the machine."""
    if CHANGE_MEASURE.check(str(row.get("state")), state, ctx={"key": key}):
        usage.set_state(key, json.dumps({**row, **extra, "state": state}))


def live_revision(sm: Any, row: Dict[str, Any]) -> Optional[str]:
    """The sha256 of the skill's live body; ``None`` when it has none."""
    text = sm.active_skill_text(row["skill_id"], user_id=row["user_id"])
    return sm._content_revision(text) if text is not None else None


def roll_back(sm: Any, row: Dict[str, Any]) -> bool:
    """Undo one change. The archive keeps the body it replaces (``create_skill``). A skill the
    change created has no earlier body, so it moves to the archive."""
    if row.get("before_sha"):
        return bool(sm.rollback_skill(row["skill_id"], user_id=row["user_id"],
                                      to_sha=row["before_sha"], created_by="curator"))
    return bool(sm.delete_skill(row["skill_id"], user_id=row["user_id"],
                                created_by="curator", aged=True))
