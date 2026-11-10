"""A cron job's declared effect and the code check of it (108 W5).

``payload.expect`` states what a run of this job must do: ``{"effect": "social", "min": 1}``
(at least ``min`` acts of that class went out in this run) or ``{"idle_ok": true}`` (a clean
run with no act is the job working). At run end the runner compares it with this run's own
``external_write`` rows and stores the result as ``verified`` on the run record and on the
``cron_run`` row.

A job with no ``expect`` has no ``verified``, and neither does a run whose rows could not be
read: an unreadable log is not a run that did nothing.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PAYLOAD_KEY = "expect"
#: The outcomes of a run that ended cleanly with nothing left undone.
_CLEAN_OUTCOMES = frozenset({"done", "skipped"})
#: A run's rows are all younger than this (the longest run cap is 30 minutes).
_RUN_WINDOW_S = 86400
_MAX_MIN = 1000


def validate_expect(value: Any) -> Optional[Dict[str, Any]]:
    """The stored form of ``value``; ``None`` for an empty value (no expect). Raises
    ``ValueError`` with the reason."""
    from core.verb_policy import EFFECT_CLASSES
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError('expect must be {"effect": "<class>", "min": <n>} or {"idle_ok": true}')
    if set(value) == {"idle_ok"}:
        if value["idle_ok"] is not True:
            raise ValueError("expect.idle_ok takes only true; to clear expect, pass {}")
        return {"idle_ok": True}
    if "idle_ok" in value or not set(value) <= {"effect", "min"} or "effect" not in value:
        raise ValueError('expect must be {"effect": "<class>", "min": <n>} or {"idle_ok": true}')
    effect = value["effect"]
    if effect not in EFFECT_CLASSES:
        raise ValueError(f"expect.effect must be one of {', '.join(sorted(EFFECT_CLASSES))}")
    minimum = value.get("min", 1)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= _MAX_MIN:
        raise ValueError(f"expect.min must be a whole number from 1 to {_MAX_MIN}")
    return {"effect": effect, "min": minimum}


def expect_line(payload: Optional[dict]) -> str:
    """One display line for the job's ``expect``."""
    expect = (payload or {}).get(PAYLOAD_KEY)
    if not isinstance(expect, dict) or not expect:
        return "expect: none (runs of this job have no verified result)"
    if expect.get("idle_ok"):
        return "expect: a run with no act is fine (verified unless the run did not end cleanly)"
    return f"expect: at least {expect.get('min', 1)} {expect.get('effect')} act(s) per run"


def _count_is_met(expect: dict, session_id: str, user_id: str) -> Optional[bool]:
    from core.effects import external_write_telemetry_enabled
    from core.event_kinds import EXTERNAL_WRITE
    from core.event_log import event_log_enabled, get_event_log
    if not (event_log_enabled() and external_write_telemetry_enabled()):
        return None
    minimum = int(expect.get("min", 1))
    rows = get_event_log().rows_where(
        kind=EXTERNAL_WRITE, effects=[expect.get("effect")],
        since_ts=time.time() - _RUN_WINDOW_S, user_ids={str(user_id or ""), ""},
        attrs_in={"outcome": "ok"}, session_id=str(session_id), limit=minimum)
    return None if rows is None else len(rows) >= minimum


def run_verified(payload: Optional[dict], *, outcome: str, session_id: Optional[str],
                 user_id: Optional[str]) -> Optional[bool]:
    """``True``/``False`` for a finished run of a job with ``expect``, else ``None``. Never
    raises."""
    try:
        expect = (payload or {}).get(PAYLOAD_KEY)
        if not isinstance(expect, dict) or not expect or not session_id:
            return None
        if expect.get("idle_ok"):
            return str(outcome or "") in _CLEAN_OUTCOMES
        return _count_is_met(expect, session_id, str(user_id or ""))
    except Exception:
        logger.warning("cron expect: rows of session %s not read", session_id, exc_info=True)
        return None


def verified_attrs(job: Any, outcome: str, session_id: Optional[str]) -> Dict[str, bool]:
    """``{"verified": <bool>}`` for a run record or a ``cron_run`` row, or ``{}``."""
    if outcome == "started":
        return {}
    verified = run_verified(getattr(job, "payload", None), outcome=outcome, session_id=session_id,
                            user_id=getattr(job, "user_id", ""))
    return {} if verified is None else {"verified": verified}
