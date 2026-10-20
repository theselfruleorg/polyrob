"""Credential preflight — a $0 skip for a cron whose DECLARED tool has a live
credential verdict (X evaluation 2026-09-26, P2-1).

A job's toolset is what it declares (``payload.tools`` > ``payload.rig`` >
``AUTONOMOUS_RIG_DEFAULT``, through :func:`core.config_policy.rigs.resolve_rig_tools`).
A ``full``/undeclared toolset is never skipped: the harness does not guess from
the task text what a job is for.

A declared tool needs a credential when it has a row in
``tools.tool_disclosure.CREDENTIAL_GATED_TOOLS`` (core rows + the packs'
``register_credential_gate`` rows). The job skips when such a tool's verdict
(``core.credential_verdicts``) is still inside its re-probe hold; once the hold
lapses ONE tick runs, which is the probe that clears the row on success.

The owner is told ONCE per verdict episode (``kind`` + ``first_seen``), never per
skipped run. Fail-OPEN: an unreadable verdict store or any error runs the tick.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

KIND_CREDENTIAL = "credential"
REASON = "credential_verdict"


def _declared_tools(payload: Dict[str, Any]) -> Optional[List[str]]:
    """The job's declared toolset, or None for "everything" (no rig / full)."""
    from core.config_policy.rigs import resolve_rig_tools
    return resolve_rig_tools(payload)


def _open_verdict(tool_id: str):
    """``(kind, label, verdict)`` when *tool_id* has a live credential verdict."""
    from tools.tool_disclosure import CREDENTIAL_GATED_TOOLS, _credential_verdict_status
    row = CREDENTIAL_GATED_TOOLS.get(tool_id)
    if row is None or _credential_verdict_status(tool_id) is None:
        return None
    from core.credential_verdicts import active
    held = active(row[0], live_only=True)
    if not held:
        return None
    return row[0], min(held, key=lambda x: x.first_seen)


# --- once per verdict episode ---------------------------------------------------

def _claim_notice(data_dir: str, user_id: str, kind: str, first_seen: float) -> bool:
    """True the FIRST time this (user, kind, episode) is claimed. Colocated in
    cron.db beside ``remedy_streak``. A store error answers False (no notice
    rather than one per run)."""
    try:
        from core.runtime_paths import cron_db_path
        from core.sqlite_util import execute_retry
        db = cron_db_path(data_dir)
        execute_retry(db, """CREATE TABLE IF NOT EXISTS credential_preflight_notice (
                                 user_id TEXT NOT NULL, kind TEXT NOT NULL,
                                 first_seen REAL NOT NULL, notified_at REAL NOT NULL,
                                 PRIMARY KEY (user_id, kind, first_seen))""")
        before = execute_retry(
            db, "SELECT 1 FROM credential_preflight_notice WHERE user_id=? AND kind=? "
                "AND first_seen=?", (user_id, kind, float(first_seen)), fetch="one")
        if before:
            return False
        execute_retry(db, "INSERT OR IGNORE INTO credential_preflight_notice "
                          "(user_id, kind, first_seen, notified_at) VALUES (?,?,?,?)",
                      (user_id, kind, float(first_seen), time.time()))
        return True
    except Exception:
        logger.warning("credential preflight: notice claim failed — not notifying", exc_info=True)
        return False


def _notice(job: Any, tool_id: str, v: Any) -> str:
    from core.credential_verdicts import since_text
    name = " ".join(str(getattr(job, "task", "") or "").split())[:60] or str(job.id)
    remedy = v.remedy or "see /status"
    return (f"⏸ I am skipping scheduled job \"{name}\" ({job.id}) at $0, and every other "
            f"job that declares the {tool_id} tool: its credential is refused since "
            f"{since_text(v)} ({v.code or 'rejected'}). Fix: {remedy}. The jobs run again once it is "
            f"fixed. I will not repeat this notice for this outage.")


def credential_skip(job: Any, payload: Dict[str, Any], *, data_dir: str):
    """A ``cron.preflight.PreflightSkip`` when the job needs a credential with an
    open verdict, else None. The skip carries ``notice`` only on the FIRST skip
    of that verdict episode. Never raises."""
    from cron.preflight import PreflightSkip
    try:
        for tool_id in _declared_tools(payload) or ():
            hit = _open_verdict(str(tool_id))
            if hit is None:
                continue
            kind, v = hit
            notice = None
            if _claim_notice(data_dir, str(getattr(job, "user_id", "") or ""), kind,
                             v.first_seen):
                notice = _notice(job, str(tool_id), v)
            return PreflightSkip(REASON, {"preflight": KIND_CREDENTIAL, "verdict": kind,
                                          "verdict_code": v.code or ""}, notice=notice)
    except Exception:
        logger.warning("credential preflight error — failing open (running the tick)",
                       exc_info=True)
    return None


__all__ = ["credential_skip", "KIND_CREDENTIAL", "REASON"]
