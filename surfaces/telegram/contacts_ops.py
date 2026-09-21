"""`/contacts` — who I have been writing to, and the transcript with one (E10).

The durable per-correspondent conversation log
(``core/surfaces/conversations.py``) has existed since 2026-07-13 and had ONE
reader: the agent's own ``contact_history`` action. The owner — the person who
actually wants to know whether a third party ever replied — had no seat at all.

Read-only by construction: this module opens the store, renders it, and writes
nothing. Approving or rejecting a correspondent stays on `/pending`, which is
where a trust decision belongs.

⚠️ A transcript is a THIRD PARTY's words. It is rendered inside the same
untrusted frame the agent's own recall uses, so neither a model reading the
scrollback later nor the owner skimming it mistakes quoted text for an
instruction from this system.

Shared: the REPL and the CLI import :func:`contacts_reply`.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: Conversations one chat listing renders, and messages one transcript shows.
_LIST_LIMIT = 20
_HISTORY_LIMIT = 10
#: A phone bubble, not a mail archive.
_MAX_CHARS = 2500

USAGE = (
    "Usage: /contacts                      — everyone I have written to\n"
    "/contacts <surface> <address>         — the transcript with one of them\n\n"
    "e.g. /contacts email someone@example.com\n"
    "Read-only. Approve or reject a contact from /pending."
)


def _store(container: Any, data_dir: str):
    """The registered conversation store, else one opened on the data home.

    ⚠️ A READ never creates the store: an absent file answers ``None`` so the
    caller can say "nothing recorded" instead of minting an empty database on
    a box that has never had a correspondent.
    """
    try:
        svc = container.get_service("conversation_store") if container else None
    except Exception as e:
        logger.debug("contacts: get_service failed: %s", e)
        svc = None
    if svc is not None:
        return svc
    path = os.path.join(data_dir, "conversations.db")
    if not os.path.isfile(path):
        return None
    from core.surfaces.conversations import ConversationStore
    return ConversationStore(path)


def contacts_reply(user_id: Optional[str], data_dir: str, args: List[str],
                   container: Any = None) -> str:
    """``/contacts [<surface> <address>]`` — one chat-ready string. Never raises."""
    if not user_id:
        return "Only the owner can read my contacts."
    tokens = [str(a) for a in (args or []) if str(a).strip()]
    if len(tokens) == 1:
        return ("Give me both halves: /contacts <surface> <address> "
                "(e.g. /contacts email someone@example.com), or bare "
                "/contacts for the listing.")

    try:
        store = _store(container, data_dir)
    except Exception as exc:
        logger.warning("contacts: store unavailable", exc_info=True)
        return (f"I could not open my conversation record ({type(exc).__name__}: "
                f"{str(exc)[:100]}). That is UNKNOWN, not 'nobody'.")
    if store is None:
        return ("I have no conversation record on this box yet — I have not "
                "written to anyone from here.")

    if not tokens:
        try:
            listing = store.format_list(user_id, limit=_LIST_LIMIT)
        except Exception as exc:
            logger.warning("contacts: listing failed", exc_info=True)
            return (f"My conversation record is unreadable ({type(exc).__name__}: "
                    f"{str(exc)[:100]}) — UNKNOWN, not 'nobody'.")
        if not listing:
            return ("No recorded conversations — I have not written to anyone "
                    "under this tenant.")
        return ("Conversations (newest first):\n" + listing +
                "\n\nOne transcript: /contacts <surface> <address>")

    surface, address = tokens[0], tokens[1]
    try:
        transcript = store.format_context(user_id, surface, address,
                                          limit=_HISTORY_LIMIT,
                                          max_chars=_MAX_CHARS)
    except Exception as exc:
        logger.warning("contacts: transcript failed", exc_info=True)
        return (f"I could not read that conversation ({type(exc).__name__}: "
                f"{str(exc)[:100]}) — UNKNOWN, not 'nothing was said'.")
    if not transcript:
        return (f"No recorded conversation with {surface}:{address} — "
                f"see /contacts for the ones I have.")
    try:
        from core.security.untrusted_wrap import wrap_untrusted
        return wrap_untrusted(f"{surface}:{address}", transcript)
    except Exception:
        logger.debug("contacts: untrusted wrap unavailable", exc_info=True)
        return transcript


__all__ = ["USAGE", "contacts_reply"]
