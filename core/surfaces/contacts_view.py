"""120 W5: the ONE read of the agent's contacts — who it wrote to, the state of each
contact, and the transcript with one of them.

Two stores, read together: the conversation log (``core/surfaces/conversations.py``,
what was said) and the correspondent registry (``core/surfaces/correspondents.py``,
whether a reply may route: ``pending``, ``active`` or ``expired``). The owner's
``/contacts`` (every seat, through ``core/surfaces/owner_reads.py``) and the
agent's ``contact_history`` action print this text; only the hint lines differ.

⚠️ A READ never creates a store: an absent file is "nothing recorded", never an
empty database. An unreadable store is said, never shown as nobody.

⚠️ A transcript is a THIRD PARTY's words, so it is always framed as untrusted
data. The listing carries addresses a third party chose (an inbound email
address), so the agent's copy frames it too (``frame_listing``).

Pure core: no import from ``agents``/``surfaces``/``tools``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: The registry state words, as the listing prints them. A rejected contact is an
#: ``expired`` row (``CorrespondentRegistry.reject``): the store keeps no separate state.
_STATE_WORDS = {"active": "active", "pending": "pending (waits for the owner)",
                "expired": "expired (rejected or timed out)"}
_STATE_RANK = {"active": 3, "pending": 2, "expired": 1}


def open_conversations(container: Any, data_dir: str):
    """The registered conversation store, else one opened on the data home, else
    ``None`` when there is no record yet (a read never creates the file)."""
    try:
        svc = container.get_service("conversation_store") if container else None
    except Exception as e:
        logger.debug("contacts: get_service failed: %s", e)
        svc = None
    if svc is not None:
        return svc
    from core.runtime_paths import store_db_path
    path = store_db_path("conversations.db", data_dir)
    if not os.path.isfile(path):
        return None
    from core.surfaces.conversations import ConversationStore
    return ConversationStore(path)


def _registry(container: Any, data_dir: str):
    try:
        svc = container.get_service("correspondent_registry") if container else None
    except Exception as e:
        logger.debug("contacts: registry service lookup failed: %s", e)
        svc = None
    if svc is not None:
        return svc
    from core.runtime_paths import correspondents_db_path
    path = correspondents_db_path(data_dir)
    if not os.path.isfile(path):
        return None
    from core.surfaces.correspondents import CorrespondentRegistry
    return CorrespondentRegistry(path)


def contact_states(container: Any, data_dir: str,
                   user_id: str) -> Dict[Tuple[str, str], str]:
    """``{(surface, address): state word}`` for this tenant's contacts. Raises when
    the registry cannot be read (the caller names it). A thread anchor holds no
    state of its own, so only base rows count; of several rows the most open wins."""
    reg = _registry(container, data_dir)
    if reg is None:
        return {}
    from core.surfaces.address_key import canonical_addr
    best: Dict[Tuple[str, str], str] = {}
    for row in reg.list(user_id=user_id):
        if row.get("provenance") == "thread":
            continue
        state = str(row.get("state") or "")
        key = (str(row.get("surface") or ""), canonical_addr(str(row.get("address") or "")))
        if _STATE_RANK.get(state, 0) > _STATE_RANK.get(best.get(key, ""), 0):
            best[key] = state
    return {k: _STATE_WORDS.get(v, v) for k, v in best.items()}


def contacts_text(user_id: Optional[str], data_dir: str, surface: str = "",
                  address: str = "", *, container: Any = None, list_limit: int = 20,
                  history_limit: int = 10, max_chars: int = 2500,
                  one_hint: str = "One transcript: /contacts <surface> <address>",
                  list_hint: str = "see /contacts for the ones I have",
                  frame_listing: bool = False) -> str:
    """The listing (no *surface*/*address*) or one transcript. Never raises."""
    if not user_id:
        return "Only the owner can read my contacts."
    try:
        store = open_conversations(container, data_dir)
    except Exception as exc:
        logger.warning("contacts: store unavailable", exc_info=True)
        return (f"I could not open my conversation record ({type(exc).__name__}: "
                f"{str(exc)[:100]}). That is UNKNOWN, not 'nobody'.")
    if store is None:
        return ("I have no conversation record on this box yet — I have not "
                "written to anyone from here.")
    from core.security.untrusted_wrap import wrap_untrusted
    if not (surface and address):
        note = ""
        try:
            states: Optional[dict] = contact_states(container, data_dir, user_id)
        except Exception as exc:
            logger.warning("contacts: registry unreadable", exc_info=True)
            states = None
            note = (f"\nThe contact states could not be read ({type(exc).__name__}) — "
                    f"UNKNOWN, so no line shows one.")
        try:
            listing = store.format_list(user_id, limit=list_limit, states=states)
        except Exception as exc:
            logger.warning("contacts: listing failed", exc_info=True)
            return (f"My conversation record is unreadable ({type(exc).__name__}: "
                    f"{str(exc)[:100]}) — UNKNOWN, not 'nobody'.")
        if not listing:
            return ("No recorded conversations — I have not written to anyone "
                    "under this tenant.")
        if frame_listing:
            listing = wrap_untrusted("contacts", listing)
        return "Conversations (newest first):\n" + listing + note + "\n\n" + one_hint
    try:
        transcript = store.format_context(user_id, surface, address,
                                          limit=history_limit, max_chars=max_chars)
    except Exception as exc:
        logger.warning("contacts: transcript failed", exc_info=True)
        return (f"I could not read that conversation ({type(exc).__name__}: "
                f"{str(exc)[:100]}) — UNKNOWN, not 'nothing was said'.")
    if not transcript:
        return f"No recorded conversation with {surface}:{address} — {list_hint}."
    return wrap_untrusted(f"{surface}:{address}", transcript)


__all__ = ["contact_states", "contacts_text", "open_conversations"]
