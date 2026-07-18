"""Per-session RunActivity snapshot (019 P1) — what is the agent doing NOW.

A tiny in-process registry mapping ``session_id`` → the CURRENT run phase,
derived at the ONE feed choke point (``ProductTelemetry._save_to_feed_directory``)
from the 019 span/wait events — no emit site calls this directly, so the
snapshot can never disagree with what the feed shows.

Phases (proposal 019 §6.2):
    idle | thinking | tool | awaiting_approval | compacting | retrying |
    delegating | done

Consumers: the session-status API (``current_activity`` field), webview badges.
In-process only — a REMOTE session (another worker owns the orchestrator)
reads ``None``, which is the honest answer. Display data; best-effort; never
raises.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_lock = threading.Lock()
_activities: Dict[str, Dict[str, Any]] = {}
_MAX_SESSIONS = 512


def _detail_for(update_type: str, data: Dict[str, Any]) -> str:
    if update_type == "llm_started":
        return str(data.get("model_name") or data.get("provider") or "")
    if update_type == "tool_started":
        return str(data.get("action_name") or data.get("tool_name") or "")
    if update_type == "awaiting_approval":
        return str(data.get("action_name") or "")
    if update_type == "compaction_started":
        return str(data.get("mode") or "")
    if update_type == "retry_wait":
        reason = data.get("reason") or "retry"
        delay = data.get("delay_sec")
        return f"{reason} ({delay:.0f}s)" if isinstance(delay, (int, float)) else str(reason)
    if update_type in ("subagent_started", "delegation_dispatched"):
        return str(data.get("goal_preview") or data.get("delegation_id") or "")
    return ""


#: update_type → phase transitions. A kind not listed leaves the phase alone.
_PHASE_STARTS = {
    "llm_started": "thinking",
    "tool_started": "tool",
    "awaiting_approval": "awaiting_approval",
    "compaction_started": "compacting",
    "retry_wait": "retrying",
    "subagent_started": "delegating",
    "delegation_dispatched": "delegating",
}
_PHASE_CLEARS = {
    # completion kind → the phase it ends (cleared only if currently in it,
    # so e.g. a tool completion can't wipe an approval wait that came after)
    "tool_execution": "tool",
    "approval_resolved": "awaiting_approval",
    "compaction_finished": "compacting",
    "subagent_finished": "delegating",
    "delegation_completed": "delegating",
}


def _put(session_id: str, value: Dict[str, Any]) -> None:
    """Insert/refresh a session's snapshot with LRU semantics (lock held).

    Pop-then-reinsert moves an EXISTING session to the end of the dict's
    insertion order, so the eviction below is least-recently-UPDATED — a
    long-lived busy session (exactly the one worth observing) can never be
    evicted by 512 short-lived ones registered after it.
    """
    if session_id in _activities:
        _activities.pop(session_id, None)
    elif len(_activities) >= _MAX_SESSIONS:
        _activities.pop(next(iter(_activities)))
    _activities[session_id] = value


def note_feed_event(
    session_id: str,
    update_type: str,
    data: Optional[Dict[str, Any]] = None,
    step: Optional[int] = None,
) -> None:
    """Fold one feed event into the session's activity snapshot. Never raises."""
    try:
        if not session_id or not update_type:
            return
        d = data if isinstance(data, dict) else {}
        with _lock:
            if update_type in _PHASE_STARTS:
                _put(session_id, {
                    "phase": _PHASE_STARTS[update_type],
                    "detail": _detail_for(update_type, d),
                    "since_ts": time.time(),
                    "step": step if isinstance(step, int) else d.get("step"),
                    "call_id": d.get("call_id"),
                })
            elif update_type in _PHASE_CLEARS:
                current = _activities.get(session_id)
                if current and current.get("phase") == _PHASE_CLEARS[update_type]:
                    _put(session_id, {
                        "phase": "idle", "detail": "", "since_ts": time.time(),
                        "step": step if isinstance(step, int) else current.get("step"),
                        "call_id": None,
                    })
            elif update_type == "session_completion":
                _put(session_id, {
                    "phase": "done", "detail": "", "since_ts": time.time(),
                    "step": None, "call_id": None,
                })
    except Exception:
        pass


def get_activity(session_id: str) -> Optional[Dict[str, Any]]:
    """The session's current activity + ``seconds_in_state``, or None (unknown/remote)."""
    try:
        with _lock:
            current = _activities.get(session_id)
            if current is None:
                return None
            out = dict(current)
        out["seconds_in_state"] = max(0.0, time.time() - out.pop("since_ts"))
        return out
    except Exception:
        return None


def _reset_for_tests() -> None:
    with _lock:
        _activities.clear()
