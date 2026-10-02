"""Who asked for a goal, and what state it is in, in the owner's words (034 §3.1/§3.2).

Two pure projections over data the board already holds. Nothing here is
written, and nothing is narrated by the model — both are DERIVED:

- :func:`origin` — the provenance axis, from payload keys ``goal_create``,
  the stream seeder and the owner seats already stamp:

  ``OWNER``   the owner launched it from a seat (``/trade``, ``polyrob goals
              create``, the console) — ``owner_granted`` or ``authored_by=owner``
              without a chat session;
  ``GRANTED`` a stream-manifest leg (``stream`` / legacy ``cycle``);
  ``ASKED``   created in a GENUINE owner chat turn (``origin_session_id`` is
              stamped only when the turn is not forged);
  ``SELF``    created by the agent in an autonomous/forged turn
              (``created_by_session_id`` alone);
  ``LEGACY``  no stamp at all (rows from before 2026-09-02).

- :func:`owner_bucket` — seven machine statuses collapse to four owner facts:
  ``NOW`` (running) · ``NEXT`` (triage/waiting/ready) · ``STUCK`` (blocked) ·
  ``OVER`` (done/cancelled). The DB keeps all seven; an unknown status renders
  as ``OTHER``, never assumed into the enum.

Pure core: stdlib + ``core.goal_vocab``.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

from core.goal_vocab import (
    STATUS_BLOCKED, STATUS_CANCELLED, STATUS_DONE, STATUS_READY, STATUS_RUNNING,
    STATUS_TRIAGE, STATUS_WAITING,
)

ORIGIN_OWNER = "OWNER"
ORIGIN_GRANTED = "GRANTED"
ORIGIN_ASKED = "ASKED"
ORIGIN_SELF = "SELF"
ORIGIN_LEGACY = "LEGACY"

#: How each origin reads on a board line (the owner's words, not the enum).
ORIGIN_TAG = {
    ORIGIN_OWNER: "you launched it",
    ORIGIN_GRANTED: "granted: {stream}",
    ORIGIN_ASKED: "you asked for it",
    ORIGIN_SELF: "I made this one up",
    ORIGIN_LEGACY: "origin not recorded",
}

BUCKET_NOW = "NOW"
BUCKET_NEXT = "NEXT"
BUCKET_STUCK = "STUCK"
BUCKET_OVER = "OVER"
BUCKET_OTHER = "OTHER"
BUCKET_ORDER = (BUCKET_NOW, BUCKET_STUCK, BUCKET_NEXT, BUCKET_OVER, BUCKET_OTHER)

_BUCKETS: Dict[str, str] = {
    STATUS_RUNNING: BUCKET_NOW,
    STATUS_TRIAGE: BUCKET_NEXT,
    STATUS_WAITING: BUCKET_NEXT,
    STATUS_READY: BUCKET_NEXT,
    STATUS_BLOCKED: BUCKET_STUCK,
    STATUS_DONE: BUCKET_OVER,
    STATUS_CANCELLED: BUCKET_OVER,
}


def _payload(goal: Any) -> Mapping[str, Any]:
    p = goal.get("payload") if isinstance(goal, Mapping) else getattr(goal, "payload", None)
    return p if isinstance(p, Mapping) else {}


def origin(goal: Any) -> str:
    """The provenance of ``goal`` (a Goal, or anything with ``payload``)."""
    p = _payload(goal)
    if p.get("owner_granted"):
        return ORIGIN_OWNER
    if p.get("stream") or p.get("cycle"):
        return ORIGIN_GRANTED
    if p.get("origin_session_id"):
        return ORIGIN_ASKED
    if str(p.get("authored_by") or "").strip().lower() == "owner":
        return ORIGIN_OWNER
    if p.get("created_by_session_id") or str(p.get("authored_by") or "").lower() == "agent":
        return ORIGIN_SELF
    return ORIGIN_LEGACY


def origin_tag(goal: Any) -> str:
    """The bracketed board tag, e.g. ``[granted: treasury-trading]``."""
    o = origin(goal)
    tag = ORIGIN_TAG[o]
    if o == ORIGIN_GRANTED:
        p = _payload(goal)
        tag = tag.format(stream=p.get("stream") or p.get("cycle") or "a stream")
    return f"[{tag}]"


def owner_bucket(status: str) -> str:
    return _BUCKETS.get(str(status or ""), BUCKET_OTHER)


def bucket_counts(status_counts: Mapping[str, int]) -> Dict[str, int]:
    """Collapse per-status counts into the owner buckets (zeros dropped)."""
    out: Dict[str, int] = {}
    for status, n in (status_counts or {}).items():
        b = owner_bucket(status)
        out[b] = out.get(b, 0) + int(n or 0)
    return {b: out[b] for b in BUCKET_ORDER if out.get(b)}


def bucket_line(status_counts: Mapping[str, int]) -> str:
    """``NOW 1 · STUCK 2 · NEXT 3 · OVER 490`` — the owner's one-line board."""
    counts = bucket_counts(status_counts)
    return " · ".join(f"{b} {n}" for b, n in counts.items())


def origin_counts(goals: Iterable[Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for g in goals or ():
        o = origin(g)
        out[o] = out.get(o, 0) + 1
    return out


__all__ = [
    "BUCKET_NEXT", "BUCKET_NOW", "BUCKET_ORDER", "BUCKET_OTHER", "BUCKET_OVER",
    "BUCKET_STUCK", "ORIGIN_ASKED", "ORIGIN_GRANTED", "ORIGIN_LEGACY", "ORIGIN_OWNER",
    "ORIGIN_SELF", "ORIGIN_TAG", "bucket_counts", "bucket_line", "origin",
    "origin_counts", "origin_tag", "owner_bucket",
]
