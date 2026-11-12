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


#: 116 W2.2: the ONE label for what a finished run did (no new stored outcome).
EFFECT = "effect"
IDLE = "idle"
MISSED = "missed"
UNVERIFIED = "unverified"
FAILED_ALL_ERRORED = "failed:all_errored"
RUN_EFFECTS = (EFFECT, IDLE, MISSED, UNVERIFIED, FAILED_ALL_ERRORED)


def _verified_word(value: Any) -> Optional[bool]:
    """``attrs["verified"]`` as one answer: a cron run's bool or a goal's word
    (``"verified"``). ``None`` when nothing checked the run. The same reading as
    ``episodic_digest.verified_state`` (core may not import agents)."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip()
    return (text == "verified") if text else None


def run_effect(attrs: Optional[dict]) -> str:
    """What a finished run did, from the attrs of its terminal ``cron_run`` /
    ``goal_run`` row. Reads exactly three keys:

    - ``all_errored_verdict`` (bool, 116 W2.1; present only when True): the run
      called done() after every substantive action errored → ``failed:all_errored``.
      It wins over the rest, in the dry mode too (the outcome then still reads ``done``).
    - ``verified`` (bool for cron — the job's ``expect`` check; the word
      ``"verified"`` for a goal — a judge or an acceptance check): ``False`` →
      ``missed``; ``True`` and not useful → ``idle`` (idle is allowed).
    - ``useful`` (bool, ``did_something``): True → ``effect``.

    Anything else → ``unverified`` (not useful and nothing checked it). The
    outcome itself is not read: call this for a run that ended ``done`` (or
    ``failed`` with the all-errored verdict). Never raises."""
    a = attrs if isinstance(attrs, dict) else {}
    if a.get("all_errored_verdict") is True:
        return FAILED_ALL_ERRORED
    verified = _verified_word(a.get("verified"))
    if verified is False:
        return MISSED
    if a.get("useful") is True:
        return EFFECT
    return IDLE if verified is True else UNVERIFIED


#: 116 W2.3: an open owner ask whose payload carries ``holds_rail`` true holds the
#: cron job its ``rail_id`` (``cron:<job id>``) names: the job does not run until
#: the ask is decided or expires. ``holds_rail_via`` says who set it: ``owner_ask``
#: (the run asked with ``holds_this_job=true``) or ``auto`` (``cron/rail_hold.py``:
#: idle runs in a row while the ask stayed open, owner decision D2).
HOLDS_RAIL_KEY = "holds_rail"
HOLD_VIA_KEY = "holds_rail_via"
HOLD_VIA_ASK = "owner_ask"
HOLD_VIA_AUTO = "auto"


def _payload_of(row: Any) -> dict:
    p = row.get("payload") if isinstance(row, dict) else getattr(row, "payload", None)
    if isinstance(p, str):
        import json
        try:
            p = json.loads(p or "{}")
        except ValueError:
            p = {}
    return p if isinstance(p, dict) else {}


def _field(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def holding_ask(open_asks: Any, job_id: str, *, now: Optional[float] = None) -> Optional[dict]:
    """The open ask that holds cron job *job_id*, or ``None``. *open_asks*: the
    tenant's OPEN asks, oldest first — ``GoalBoard.asks`` rows or ``goals``
    table rows (``id``, ``title``, ``payload`` as a dict or JSON text). An ask past
    its ``payload.expires_at`` holds nothing. Returns ``{"ask_id", "question",
    "via" ("owner_ask" | "auto"), "expires_at" (epoch or None)}``. Pure."""
    now = time.time() if now is None else float(now)
    rail = f"cron:{job_id}"
    for row in open_asks or []:
        p = _payload_of(row)
        if p.get("rail_id") != rail or p.get(HOLDS_RAIL_KEY) is not True:
            continue
        exp = p.get("expires_at")
        try:
            if exp is not None and float(exp) <= now:
                continue
        except (TypeError, ValueError):
            exp = None
        return {"ask_id": str(_field(row, "id") or ""), "question": str(_field(row, "title") or ""),
                "via": str(p.get(HOLD_VIA_KEY) or HOLD_VIA_ASK),
                "expires_at": float(exp) if exp is not None else None}
    return None


def job_held_by(goals_db: str, user_id: str, job_id: str, *,
                now: Optional[float] = None) -> Optional[dict]:
    """:func:`holding_ask` read from ``goals.db``: the open question that holds
    cron job *job_id* of *user_id*, or ``None`` (none, or no ``goals.db`` yet).
    RAISES when the store exists and cannot be read — an unreadable board is not
    "nothing holds it" (the scheduler then runs the job; status says it could not read)."""
    import os
    if not goals_db or not os.path.exists(goals_db):
        return None
    from core.goal_vocab import ASK_OPEN, KIND_ASK
    from core.sqlite_util import execute_retry
    rows = execute_retry(
        goals_db, "SELECT id, title, payload FROM goals WHERE kind=? AND status=? "
                  "AND user_id=? ORDER BY created_at",
        (KIND_ASK, ASK_OPEN, str(user_id or "")), fetch="all") or []
    return holding_ask([dict(r) for r in rows], job_id, now=now)


def verified_attrs(job: Any, outcome: str, session_id: Optional[str]) -> Dict[str, bool]:
    """``{"verified": <bool>}`` for a run record or a ``cron_run`` row, or ``{}``."""
    if outcome == "started":
        return {}
    verified = run_verified(getattr(job, "payload", None), outcome=outcome, session_id=session_id,
                            user_id=getattr(job, "user_id", ""))
    return {} if verified is None else {"verified": verified}
