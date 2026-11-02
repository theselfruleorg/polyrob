"""094: the per-session half of a model pin that is not the model itself — the
reasoning level — plus a pin queued for a chat that has no session yet.

The pinned MODEL lives in the session record (``request.model/provider``), so a
recreate builds on it. The reasoning level rides the record too (``model_pin``)
and is mirrored here so the main-model call (``llm_runner.get_next_action``) can
read it without touching disk; a recreated session re-learns it through
:func:`remember_session_pin`. So does the owner's switch itself (095 W1a): the
pin carries ``switched_from`` + ``set_at``, so a reload re-renders "The owner
switched you … at HH:MMZ" instead of losing it.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

_REASONING: Dict[str, Optional[str]] = {}
#: session id -> (model, switched_from, epoch) of the owner's last switch.
_SWITCH: Dict[str, Tuple[str, str, float]] = {}
#: chat session key -> {"provider", "model", "reasoning"} for a chat with no
#: bound session; the next cold start of that chat consumes it (in memory only).
_PENDING_CHAT: Dict[str, Dict[str, Any]] = {}


def set_session_reasoning(session_id: Optional[str], level: Optional[str]) -> None:
    if session_id:
        _REASONING[str(session_id)] = level or None


def session_reasoning(session_id: Optional[str]) -> Optional[str]:
    return _REASONING.get(str(session_id)) if session_id else None


def remember_session_pin(session_id: Optional[str], session_info: Optional[dict]) -> None:
    """Re-learn a recreated session's reasoning level from its record."""
    pin = (session_info or {}).get("model_pin")
    if not (session_id and isinstance(pin, dict)):
        return
    if pin.get("reasoning"):
        _REASONING[str(session_id)] = str(pin["reasoning"])
    if pin.get("model") and pin.get("switched_from") and pin["switched_from"] != pin["model"]:
        note_switch(session_id, str(pin["model"]), str(pin["switched_from"]),
                    float(pin.get("switched_at") or pin.get("set_at") or 0) or None)
    if str(pin.get("provider") or "").lower() == "openrouter" and pin.get("model"):
        # The live window/price of a pinned OpenRouter id (memory or disk cache only).
        try:
            from modules.llm.openrouter_catalog import cached_entry, register_live_model
            entry = cached_entry(str(pin["model"]))
            if entry is not None:
                register_live_model(entry)
        except Exception:
            pass


def note_switch(session_id: Optional[str], model: str, switched_from: str,
                at: Optional[float] = None) -> None:
    """Remember the owner's switch to ``model`` (``at`` = epoch, default now)."""
    if session_id and model and switched_from and switched_from != model:
        import time
        _SWITCH[str(session_id)] = (model, switched_from, float(at or time.time()))


def session_switch(session_id: Optional[str], model: Optional[str]) -> Optional[Tuple[str, float]]:
    """``(switched_from, epoch)`` when the session runs the model the owner
    switched it to, else None (a later fallback or reset is not that switch)."""
    entry = _SWITCH.get(str(session_id)) if session_id else None
    if not entry or entry[0] != model:
        return None
    return entry[1], entry[2]


def forget_session(session_id: Optional[str]) -> None:
    if session_id:
        _REASONING.pop(str(session_id), None)
        _SWITCH.pop(str(session_id), None)


def queue_chat_pin(chat_key: Optional[str], pin: Dict[str, Any]) -> None:
    if chat_key:
        _PENDING_CHAT[str(chat_key)] = dict(pin)


def take_chat_pin(chat_key: Optional[str]) -> Optional[Dict[str, Any]]:
    return _PENDING_CHAT.pop(str(chat_key), None) if chat_key else None


def peek_chat_pin(chat_key: Optional[str]) -> Optional[Dict[str, Any]]:
    return _PENDING_CHAT.get(str(chat_key)) if chat_key else None
