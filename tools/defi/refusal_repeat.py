"""Say it plainly when a refused money call is sent again unchanged (review
2026-10-13 C-09).

Prod d0ee1d13 (2026-10-08): a revoke was refused by the guard; the agent wrote
"revoke with an adequate cap" and sent the SAME arguments again; the identical
refusal came back as "[duplicate of an earlier tool result …]" and the run
closed BLOCKED with a live allowance. The second refusal now says that the
arguments were identical and that only a changed argument can change the
answer.

Keyed per RUN (:func:`run_key`) on the guard intent with its
idempotency key removed (that key carries the step). Process-local and bounded:
a miss only loses the note, never changes a decision.
"""
from __future__ import annotations

import dataclasses
import hashlib
import threading
from collections import OrderedDict
from typing import Any, Optional

#: How many refused calls are remembered (oldest first out).
MAX_KEYS = 512

REPEAT_NOTE = ("  SAME ARGUMENTS as a call the guard already refused in this run — "
               "identical arguments get the identical refusal. Change the argument "
               "the guard line names (for example max_spend_usd or the amount), or "
               "stop and tell the owner; do not send this call again unchanged.")

_LOCK = threading.Lock()
_REFUSED: "OrderedDict[str, str]" = OrderedDict()


def run_key(session_id: Optional[str]) -> str:
    """The RUN a session id belongs to: a sub-agent's virtual id
    (``{parent}__{child}``, ``sub_agent_manager._generate_virtual_session_id``)
    collapses to its parent."""
    sid = str(session_id or "").strip()
    return sid.split("__", 1)[0] if sid else ""


def _key(execution_context: Any, verb: str, intent: Any) -> Optional[str]:
    run = run_key(getattr(execution_context, "session_id", None))
    if not run:
        return None
    try:
        shape = (dataclasses.replace(intent, idempotency_key=None)
                 if dataclasses.is_dataclass(intent)
                 and any(f.name == "idempotency_key" for f in dataclasses.fields(intent))
                 else intent)
        material = f"{run}|{verb}|{shape!r}"
    except Exception:
        return None
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


def note_refusal(execution_context: Any, verb: str, intent: Any, reason: str) -> bool:
    """Record one guard refusal. True when the SAME call was refused before
    in this run FOR THE SAME REASON (the caller adds :data:`REPEAT_NOTE`)."""
    key = _key(execution_context, verb, intent)
    if key is None:
        return False
    reason = str(reason or "")[:200]
    with _LOCK:
        # Same call AND same refusal: a transient refusal (an RPC or simulation
        # fault, a cap the owner just raised) retried unchanged may legitimately
        # get a different answer, and must not be told "identical refusal".
        seen = _REFUSED.get(key) == reason
        _REFUSED[key] = reason
        _REFUSED.move_to_end(key)
        while len(_REFUSED) > MAX_KEYS:
            _REFUSED.popitem(last=False)
    return seen


def reset_for_tests() -> None:
    with _LOCK:
        _REFUSED.clear()


__all__ = ["MAX_KEYS", "REPEAT_NOTE", "note_refusal", "reset_for_tests"]
