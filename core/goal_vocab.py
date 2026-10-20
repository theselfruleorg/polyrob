"""The goal board's vocabulary — status, kind and ask literals.

The board (``agents/task/goals/board.py``) owns the semantics; the LITERALS
live here, in the core tier, because five modules below and beside it need
them and ``core/`` may not import ``agents.*``: the status snapshot and the
board renderer re-declared them by hand (pinned equal by a contract test),
and the live-status set was spelled out three times (goal_list, the
snapshot, the renderer). One spelling, imported everywhere.
"""
from __future__ import annotations

import re

# goal lifecycle
STATUS_TRIAGE = "triage"
STATUS_WAITING = "waiting"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
#: 080 D3: the OWNER held it (``/goal pause``). Never dispatched (the dispatcher
#: reads ``ready`` only); ``/goal retry`` lifts it. Never ``blocked`` for a pause:
#: blocked means it failed, and every view reads it as "fix the cause".
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

#: Work that is still on the board, in the order a listing shows it.
#: ``done``/``cancelled`` are history and dwarf the live rows within days
#: (357 done vs 1 ready on prod, 2026-08-29).
LIVE_STATUS_ORDER = (STATUS_TRIAGE, STATUS_WAITING, STATUS_READY,
                     STATUS_RUNNING, STATUS_BLOCKED, STATUS_PAUSED)
LIVE_STATUSES = frozenset(LIVE_STATUS_ORDER)

# kind: rows are goals (dispatchable) or asks (owner-facing needs, never
# dispatched)
KIND_GOAL = "goal"
KIND_ASK = "ask"

# ask lifecycle — disjoint from goal statuses so nothing dispatches them
ASK_OPEN = "open"
ASK_FULFILLED = "fulfilled"
ASK_REJECTED = "rejected"
ASK_OBSOLETE = "obsolete"

# ask kinds (``payload.ask_kind``) that are DECIDED on /pending by their own
# decider, never by /fulfill or owner_ask(answer=): a tool/spend approval and
# the W1 token-identity question (which contract is the real token). Every
# generic ask listing skips them, so one decision is never shown twice.
ASK_KIND_TOOL_APPROVAL = "tool_approval"
ASK_KIND_TOKEN_IDENTITY = "token_identity"
OWN_SURFACE_ASK_KINDS = frozenset({ASK_KIND_TOOL_APPROVAL, ASK_KIND_TOKEN_IDENTITY})


def has_own_surface(payload) -> bool:
    """True when an ask with this payload is decided on /pending, not /fulfill."""
    return isinstance(payload, dict) and payload.get("ask_kind") in OWN_SURFACE_ASK_KINDS

def normalize_title(title: str) -> str:
    """The ONE title key: lowercase, alnum words, single spaces. The board's
    dedup, the suppression store and the renderers all compare titles by it."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).split())
