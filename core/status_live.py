"""Live actor read model for status_snapshot; no surface-specific policy or store."""
def _iso_ts(raw) -> "float | None":
    """An epoch float from an ISO text (cron's ``last_run_at``) or None."""
    if raw in (None, ""):
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _first_line(text, limit: int = 160) -> str:
    line = str(text or "").strip().splitlines()
    head = line[0].strip() if line else ""
    return head if len(head) <= limit else head[: limit - 1] + "…"


def build_live_status(user_id: str, data_dir: str, sessions_root: str) -> dict:
    import json as _json

    import os

    from core.status_snapshot import _pid_alive, _rows
    out: dict = {"goals": None, "cron": None, "sessions": None,
                 "unreadable": {}, "count": None, "user_id": user_id}
    try:
        rows = _rows(os.path.join(data_dir, "goals.db"),
                     "SELECT id, title, started_at, session_id FROM goals "
                     "WHERE status='running' AND kind='goal' AND user_id=? "
                     "ORDER BY started_at DESC", (user_id,))
        out["goals"] = [{"id": r.get("id"), "title": r.get("title"),
                         "since": r.get("started_at"), "session_id": r.get("session_id")}
                        for r in rows]
    except Exception as exc:
        out["unreadable"]["goals"] = f"{type(exc).__name__}: {exc}"[:200]
    try:
        rows = _rows(os.path.join(data_dir, "cron.db"),
                     "SELECT id, task, last_run_at FROM cron_jobs "
                     "WHERE status='running' AND user_id=?", (user_id,))
        out["cron"] = [{"id": r.get("id"), "task": _first_line(r.get("task")),
                        "since": _iso_ts(r.get("last_run_at"))} for r in rows]
    except Exception as exc:
        out["unreadable"]["cron"] = f"{type(exc).__name__}: {exc}"[:200]
    try:
        rows = _rows(os.path.join(data_dir, "session_registry.db"),
                     "SELECT session_id, worker_pid, created_at, last_seen_at "
                     "FROM active_sessions")
        live = []
        for r in rows:
            sid = str(r.get("session_id") or "")
            pid = r.get("worker_pid")
            if not sid or not _pid_alive(int(pid or 0)):
                continue
            sdir = os.path.join(sessions_root, user_id, sid)
            if not os.path.isdir(sdir):
                continue  # another tenant's, or gone
            task, creator, status = "", None, None
            try:
                with open(os.path.join(sdir, "task.json"), encoding="utf-8") as fh:
                    tj = _json.load(fh)
                task = _first_line(tj.get("task"))
                creator = tj.get("creator") or tj.get("created_by")
            except Exception as exc:
                out["unreadable"][f"session:{sid}:task"] = str(exc)
            try:
                with open(os.path.join(sdir, "status.json"), encoding="utf-8") as fh:
                    status = _json.load(fh).get("status")
            except Exception as exc:
                out["unreadable"][f"session:{sid}:status"] = str(exc)
            live.append({"session_id": sid, "task": task, "creator": creator,
                         "status": status, "since": _iso_ts(r.get("created_at")),
                         "owner_pid": pid})
        out["sessions"] = live
    except Exception as exc:
        out["unreadable"]["sessions"] = f"{type(exc).__name__}: {exc}"[:200]
    if any(out[key] is not None for key in ("goals", "cron", "sessions")):
        out["count"] = (len(out["goals"] or []) + len(out["cron"] or [])
                        + len(out["sessions"] or []))
    out["count_unit"] = "actors"
    return out
