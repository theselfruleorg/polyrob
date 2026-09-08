"""032 — the owner verbs every seat renders from (CLI, REPL, Telegram, console).

One decision helper per verb; every seat calls these and prints the returned
text, so the four seats cannot drift (the 031 lesson: one verified rendering).
"""
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from core.app_service.config import logs_path
from core.app_service.fingerprint import describe_change
from core.app_service.registry import (
    STATUS_PENDING, STATUS_STOPPED, AppServiceRegistry,
)

APPROVE_HINTS = "polyrob apps approve <slug> · /apps approve <slug> · console → Apps"


def _emit(kind: str, user_id: str, attrs: Dict[str, Any]) -> None:
    try:
        from core.event_log import event_log_enabled, get_event_log
        if event_log_enabled():
            get_event_log().record(kind, user_id=user_id, source="app_owner", attrs=attrs)
    except Exception:
        pass


def _hhmm(ts: Optional[float]) -> str:
    try:
        return time.strftime("%H:%M UTC", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return "-"


def pending_reason(row: Dict[str, Any]) -> Optional[str]:
    """Why an already-approved address is pending again (which fields moved), or
    ``None`` for a first approval / a non-pending row."""
    if row.get("status") != STATUS_PENDING or not row.get("approved_at"):
        return None
    fields = list(row.get("approval_change") or [])
    return describe_change(fields) if fields else None


def where(row: Dict[str, Any]) -> str:
    if row.get("public_url"):
        return str(row["public_url"])
    if row["status"] == STATUS_PENDING:
        return "pending your approval"
    if row.get("host_port"):
        return f"http://127.0.0.1:{row['host_port']} (loopback only)"
    return "not running"


def row_json(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("slug", "user_id", "status", "source_dir", "cmd", "container_port", "health_path",
            "egress", "egress_allow", "env", "workspace_digest", "host_port", "container_name",
            "public_url", "approved_at", "last_deploy", "last_health", "consecutive_failures",
            "last_failure_error", "approval_change", "created_at", "updated_at")
    out = {k: row.get(k) for k in keys}
    out["where"] = where(row)
    out["pending_reason"] = pending_reason(row)
    return out


def list_lines(registry: AppServiceRegistry, user_id: str) -> List[str]:
    rows = registry.list_for(user_id)
    if not rows:
        return ["No apps."]
    lines = [f"{len(rows)} app(s):"]
    for r in rows:
        tail = ""
        reason = pending_reason(r)
        if reason:
            tail = f" — {reason}; re-approval needed"
        elif r.get("last_failure_error") and r["status"] in ("failed", "stopped", "paused"):
            tail = f" — {str(r['last_failure_error'])[:100]}"
        elif r.get("last_health"):
            tail = f" (health {_hhmm(r['last_health'])})"
        lines.append(f"- {r['slug']} [{r['status']}] {where(r)}{tail}")
    if any(r["status"] == STATUS_PENDING for r in rows):
        lines.append(f"approve: {APPROVE_HINTS}")
    return lines


def show_lines(registry: AppServiceRegistry, user_id: str, slug: str) -> List[str]:
    r = registry.get(slug, user_id)
    if r is None:
        return [f"no app {slug!r} for this tenant"]
    reason = pending_reason(r)
    return [
        f"{r['slug']} [{r['status']}] {where(r)}"
        + (f" — {reason} since approval; re-approval needed" if reason else ""),
        f"   dir {r['source_dir']}",
        f"   cmd {' '.join(r['cmd'])}  port {r['container_port']}  health {r['health_path']}",
        f"   egress {r['egress']}" + (f" ({', '.join(r['egress_allow'])})" if r.get("egress_allow") else ""),
        f"   env {', '.join(f'{k}={v}' for k, v in (r.get('env') or {}).items()) or '-'}",
        f"   digest {str(r.get('workspace_digest') or '')[:12] or '-'}  approved {_hhmm(r.get('approved_at')) if r.get('approved_at') else 'no'}"
        f"  deployed {_hhmm(r.get('last_deploy')) if r.get('last_deploy') else '-'}  health {_hhmm(r.get('last_health')) if r.get('last_health') else '-'}",
        f"   failures {r.get('consecutive_failures') or 0}" + (f" — {r['last_failure_error']}" if r.get("last_failure_error") else ""),
    ]


def approve(registry: AppServiceRegistry, slug: str, user_id: str, *, via: str) -> Tuple[bool, str]:
    r = registry.get(slug, user_id)
    if r is None:
        return False, f"no app {slug!r} for this tenant"
    if r["status"] != STATUS_PENDING:
        return False, f"{slug!r} is {r['status']}, not pending (approval sticks to the address)"
    if not registry.mark_approved(slug, user_id):
        return False, f"{slug!r} could not be approved (state changed underneath)"
    from core.event_kinds import APP_APPROVED
    _emit(APP_APPROVED, user_id, {"slug": slug, "by": "owner", "via": via})
    return True, (f"✅ {slug!r} approved — the supervisor deploys it within one tick; "
                  f"`polyrob apps show {slug}` / /apps show {slug} for the URL and health.")


def reject(registry: AppServiceRegistry, slug: str, user_id: str, *, via: str) -> Tuple[bool, str]:
    r = registry.get(slug, user_id)
    if r is None:
        return False, f"no app {slug!r} for this tenant"
    if r["status"] != STATUS_PENDING:
        return False, f"{slug!r} is {r['status']}, not pending — use kill to stop a running app"
    registry.set_status(slug, user_id, STATUS_STOPPED, error=f"rejected by owner via {via}")
    from core.event_kinds import APP_STOPPED
    _emit(APP_STOPPED, user_id, {"slug": slug, "by": "owner", "via": via, "rejected": True})
    return True, f"✖ {slug!r} rejected; it never ran."


def kill(registry: AppServiceRegistry, slug: str, user_id: str, *, via: str) -> Tuple[bool, str]:
    r = registry.get(slug, user_id)
    if r is None:
        return False, f"no app {slug!r} for this tenant"
    if r["status"] == STATUS_STOPPED:
        return True, f"{slug!r} is already stopped."
    registry.set_status(slug, user_id, STATUS_STOPPED, error=f"killed by owner via {via}")
    from core.event_kinds import APP_STOPPED
    _emit(APP_STOPPED, user_id, {"slug": slug, "by": "owner", "via": via})
    return True, (f"⏹ {slug!r} stop queued — the supervisor removes its container and stanza "
                  f"within one tick (the address stays approved for a later redeploy).")


def logs_tail(data_dir: Optional[str], user_id: str, slug: str, n: int = 50) -> str:
    path = logs_path(data_dir, user_id, slug)
    if not os.path.isfile(path):
        return "(no logs yet — the supervisor refreshes them every tick while the app is live)"
    n = max(1, min(int(n), 400))
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return "".join(fh.readlines()[-n:]) or "(empty)"
