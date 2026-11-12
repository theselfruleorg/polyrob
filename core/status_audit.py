"""The status audit: recount what ``/status`` prints (proposal 116 W10.1).

The 2026-11-11 review compared the status text with the stores by hand and
found twenty-five numbers that did not match. This module does that
comparison in code: it builds the ONE snapshot, then reads each count again
with a direct, independent query (no status helper, no shared predicate
beyond the tenant rule) and returns every pair. ``polyrob doctor
--audit-status`` prints them; the maintenance routine runs it once a day.

A check has three results: ``ok`` (the numbers agree), ``differs`` and
``not checked`` (one side could not be read — the reason is on the row). The
audit never changes a store and never raises: a failed check is a row.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Callable, Dict, List, Optional

OK, DIFFERS, NOT_CHECKED = "ok", "differs", "not checked"


def _ro(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise FileNotFoundError(f"{os.path.basename(path)} not found at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _all(path: str, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    conn = _ro(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _runs_by_outcome(tele_db: str, kind: str, user_id: str, since: float,
                     until: float) -> Dict[str, int]:
    rows = _all(tele_db,
                "SELECT attrs FROM telemetry_events WHERE kind = ? AND ts >= ? AND ts <= ? "
                "AND (user_id = ? OR user_id = '')", (kind, since, until, user_id))
    out: Dict[str, int] = {}
    for r in rows:
        try:
            outcome = (json.loads(r["attrs"] or "{}") or {}).get("outcome") or "?"
        except ValueError:
            outcome = "?"
        out[outcome] = out.get(outcome, 0) + 1
    return out


def _section(snap: Any, name: str) -> Any:
    sec = snap.sections.get(name)
    if sec is None or not sec.available:
        raise LookupError(f"the {name} section is unavailable"
                          + (f" ({sec.reason})" if sec is not None and sec.reason else ""))
    return sec


def audit_status(user_id: str, data_dir: str, *, snapshot: Any = None) -> List[Dict[str, Any]]:
    """``[{"check", "snapshot", "direct", "result", "note"}]``, one row per count."""
    from core.runtime_paths import cron_db_path, goals_db_path
    from core.status_snapshot import build_status_snapshot
    from core.status_types import _telemetry_db_path

    snap = snapshot or build_status_snapshot(str(user_id), data_dir=data_dir)
    until = float(snap.generated_at)
    since = until - float(snap.window_sec)
    tele_db, goals_db = _telemetry_db_path(data_dir), goals_db_path(data_dir)
    rows: List[Dict[str, Any]] = []

    def check(name: str, shown: Callable[[], Any], direct: Callable[[], Any]) -> None:
        row: Dict[str, Any] = {"check": name, "snapshot": None, "direct": None,
                               "result": NOT_CHECKED, "note": ""}
        try:
            row["snapshot"] = shown()
        except Exception as e:
            row["note"] = f"snapshot: {type(e).__name__}: {str(e)[:100]}"
            rows.append(row)
            return
        try:
            row["direct"] = direct()
        except Exception as e:
            row["note"] = f"direct read: {type(e).__name__}: {str(e)[:100]}"
            rows.append(row)
            return
        row["result"] = OK if row["snapshot"] == row["direct"] else DIFFERS
        rows.append(row)

    def _job_runs_shown() -> Dict[str, int]:
        data = _section(snap, "loops").data
        if "cron_runs_window" not in data:
            raise LookupError("the loops section read no event log")
        out = dict(data["cron_runs_window"])
        if data.get("cron_starts_window"):
            out["started"] = data["cron_starts_window"]
        return out

    def _job_runs_direct() -> Dict[str, int]:
        out = _runs_by_outcome(tele_db, "cron_run", user_id, since, until)
        out.pop("provider_rerouted", None)          # a marker, not a run
        return out

    def _goal_runs_shown() -> Dict[str, int]:
        data = _section(snap, "work").data
        if "runs_window" not in data:
            raise LookupError("the work section read no event log")
        out = dict(data["runs_window"])
        if data.get("run_starts_window"):
            out["started"] = data["run_starts_window"]
        return out

    check("job runs by outcome (24h)", _job_runs_shown, _job_runs_direct)
    check("goal runs by outcome (24h)", _goal_runs_shown,
          lambda: _runs_by_outcome(tele_db, "goal_run", user_id, since, until))
    check("goals by status",
          lambda: {k: v for k, v in _section(snap, "work").data["counts"].items() if v},
          lambda: {r["status"]: int(r["n"]) for r in _all(
              goals_db, "SELECT status, COUNT(*) AS n FROM goals WHERE kind = 'goal' "
                        "AND user_id = ? GROUP BY status", (user_id,))})
    check("enabled jobs",
          lambda: _section(snap, "loops").data["cron_enabled"],
          lambda: int(_all(cron_db_path(data_dir),
                           "SELECT COUNT(*) AS n FROM cron_jobs WHERE user_id = ? "
                           "AND enabled = 1", (user_id,))[0]["n"]))

    from core.status_delivery import _outbound_queue_db
    outbox = _outbound_queue_db(data_dir)

    def _queue_direct() -> Dict[str, Any]:
        got = _all(outbox, "SELECT COUNT(*) AS n, MIN(created_at) AS oldest "
                           "FROM outbound_queue WHERE state = 'pending'")[0]
        return {"pending": int(got["n"] or 0), "oldest": got["oldest"]}

    def _queue_shown() -> Dict[str, Any]:
        data = _section(snap, "delivery").data
        if "outbound_queue" not in data:
            raise LookupError("the delivery section read no outbound queue")
        return {"pending": int((data["outbound_queue"] or {}).get("pending", 0)),
                "oldest": data.get("outbound_oldest_pending_ts")}

    check("outbound pending rows and the oldest", _queue_shown, _queue_direct)

    def _lag_direct() -> Optional[int]:
        got = _all(tele_db,
                   "SELECT MAX(CAST(json_extract(attrs, '$.loop_lag_ms_max') AS INTEGER)) AS m "
                   "FROM telemetry_events WHERE kind = 'autonomy_tick' AND ts >= ? AND ts <= ?",
                   (since, until))
        return None if got[0]["m"] is None else int(got[0]["m"])

    check("event-loop lag maximum (24h, ms)",
          lambda: _section(snap, "loops").data.get("loop_lag_ms_max"), _lag_direct)

    def _ticks_direct() -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {}
        for r in _all(tele_db,
                      "SELECT attrs FROM telemetry_events WHERE kind = 'autonomy_tick' "
                      "AND ts >= ? AND ts <= ? ORDER BY ts DESC", (since, until)):
            a = json.loads(r["attrs"] or "{}") or {}
            loop = a.get("loop") or "?"
            if a.get("tick_ok_at") is not None:
                out[loop] = max(out.get(loop) or 0.0, float(a["tick_ok_at"]))
            else:
                out.setdefault(loop, None)
        return out

    def _ticks_shown() -> Dict[str, Optional[float]]:
        beats = _section(snap, "loops").data.get("heartbeats")
        if beats is None:
            raise LookupError("the loops section read no heartbeat")
        return {loop: hb.get("tick_ok_at") for loop, hb in beats.items()}

    check("last completed tick per loop", _ticks_shown, _ticks_direct)

    def _positions_shown() -> int:
        import re
        for line in _section(snap, "money").lines:
            if line.startswith("open positions:"):
                if "none in the rail book" in line:
                    return 0
                got = re.search(r"open positions: (\d+) open", line)
                if got:
                    return int(got.group(1))
                raise LookupError(line[:80])
        raise LookupError("the money section prints no open-position line")

    def _positions_direct() -> int:
        from core.open_positions import open_positions_db_path
        book = open_positions_db_path(data_dir)
        if not os.path.exists(book):
            return 0
        return int(_all(book, "SELECT COUNT(*) AS n FROM open_positions WHERE user_id = ? "
                              "AND status = 'open' AND qty > 0", (user_id,))[0]["n"])

    check("open positions", _positions_shown, _positions_direct)
    return rows


def render_audit(rows: List[Dict[str, Any]]) -> List[str]:
    """The audit as lines: every disagreement first, then what agreed."""
    bad = [r for r in rows if r["result"] == DIFFERS]
    unknown = [r for r in rows if r["result"] == NOT_CHECKED]
    good = [r for r in rows if r["result"] == OK]
    out = [f"Status audit: {len(bad)} differ(s), {len(good)} agree, "
           f"{len(unknown)} not checked"]
    for r in bad:
        out.append(f"  DIFFERS  {r['check']}: status says {r['snapshot']!r}, "
                   f"the store says {r['direct']!r}")
    for r in unknown:
        out.append(f"  not checked  {r['check']}: {r['note']}")
    for r in good:
        out.append(f"  ok  {r['check']}: {r['direct']!r}")
    return out
