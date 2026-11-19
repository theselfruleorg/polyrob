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

Shared: the REPL and the CLI import :func:`contacts_reply`. 120 W5: the read itself
lives in ``core/surfaces/contacts_view.py``; this module parses the words.
"""
from __future__ import annotations

from typing import Any, List, Optional

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


def contacts_reply(user_id: Optional[str], data_dir: str, args: List[str],
                   container: Any = None) -> str:
    """``/contacts [<surface> <address>]`` — one chat-ready string. Never raises.

    The seat parses the words; ``core.surfaces.contacts_view.contacts_text`` holds
    the read, the contact state and the untrusted frame (the agent's
    ``contact_history`` prints the same text)."""
    tokens = [str(a) for a in (args or []) if str(a).strip()]
    if user_id and len(tokens) == 1:
        return ("Give me both halves: /contacts <surface> <address> "
                "(e.g. /contacts email someone@example.com), or bare "
                "/contacts for the listing.")
    from core.surfaces.contacts_view import contacts_text
    surface, address = (tokens[0], tokens[1]) if len(tokens) >= 2 else ("", "")
    return contacts_text(user_id, data_dir, surface, address, container=container,
                         list_limit=_LIST_LIMIT, history_limit=_HISTORY_LIMIT,
                         max_chars=_MAX_CHARS)


__all__ = ["USAGE", "contacts_reply"]
