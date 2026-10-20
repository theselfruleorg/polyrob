"""``prompt_manifest`` — what went into a session's prompt (2026-10-14 review D-10).

Only recall rows and skills were recorded; the injected previous-runs, live
health, owner thread and rules were not, so "did the model have X?" needed the
session feed (7 days) or a guess. One event per session start says which inputs
the prompt carried and how big each was — sizes and ids only, never the text.

The assembly is spread over the agent's construction and its first step, so
this module is the ONE builder + emitter; the call site passes what it has
(today: ``Agent._log_context_breakdown`` on step 1, from the ONE context gauge
``MessageManager.context_usage()`` — slot sizes in tokens):

    from core.prompt_manifest import emit_prompt_manifest
    emit_prompt_manifest(session_id=…, user_id=…, regime=…,
                         slots={"foundation": 31000, "tool_schemas": 55000, …},  # tokens
                         skills=["exits"], recall_rows=3,
                         previous_runs_chars=1200, live_health_chars=800)

Once per (session, phase) per process; fail-open (telemetry never breaks a run).
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

_SEEN: set = set()
_LOCK = threading.Lock()
#: Bound on the once-per-session memory (a long-lived server process).
_SEEN_MAX = 5000


def build_prompt_manifest(*, regime: str = "", model: str = "",
                          slots: Optional[Dict[str, Any]] = None,
                          skills: Optional[Iterable[str]] = None,
                          recall_rows: Optional[int] = None,
                          previous_runs_chars: Optional[int] = None,
                          live_health_chars: Optional[int] = None,
                          phase: str = "start",
                          extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The attrs dict. A slot whose size is not an int is dropped; ``None`` for
    a count means "not measured here" and is left out (never a zero)."""
    clean_slots = {str(k): int(v) for k, v in (slots or {}).items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}
    out: Dict[str, Any] = {"phase": phase, "slots": clean_slots,
                           "total_tokens": sum(clean_slots.values())}
    if regime:
        out["regime"] = regime
    if model:
        out["model"] = model
    if skills is not None:
        out["skills"] = sorted({str(s) for s in skills if s})[:50]
    for key, val in (("recall_rows", recall_rows), ("previous_runs_chars", previous_runs_chars),
                     ("live_health_chars", live_health_chars)):
        if val is not None:
            out[key] = int(val)
    if extra:
        out.update({k: v for k, v in extra.items() if k not in out})
    if isinstance(out.get("prompt_tokens"), int):
        # The slots are chars/N estimates; the provider's count is the truth. The
        # signed gap names what the estimate missed (4-9% on prod, 081 re-measure).
        out["estimate_gap"] = out["prompt_tokens"] - out["total_tokens"]
    return out


def emit_prompt_manifest(*, session_id: str, user_id: str = "", phase: str = "start",
                         **fields: Any) -> bool:
    """Record ONE ``prompt_manifest`` row for ``(session_id, phase)``. Returns
    True when it was emitted now, False when already emitted or on any error."""
    sid = str(session_id or "")
    if not sid:
        return False
    key = (sid, phase)
    with _LOCK:
        if key in _SEEN:
            return False
        if len(_SEEN) >= _SEEN_MAX:
            _SEEN.clear()
        _SEEN.add(key)
    try:
        from core.event_kinds import PROMPT_MANIFEST
        from core.event_log import emit
        emit(PROMPT_MANIFEST, source="prompt", user_id=user_id, session_id=sid,
             attrs=build_prompt_manifest(phase=phase, **fields))
        return True
    except Exception:
        logger.debug("prompt_manifest skipped for %s", sid, exc_info=True)
        return False


__all__ = ["build_prompt_manifest", "emit_prompt_manifest"]
