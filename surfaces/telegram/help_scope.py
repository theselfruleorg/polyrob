"""Who reads which help (113 W7 F-05).

``/help`` printed the owner's whole verb catalog to anyone who could DM the
agent — a paired person, a correspondent — and each verb then answered
"Owner only". The "/" menu went to every allowlisted Telegram id, though only
the owner's own id runs the verbs (``owner_surface_alias`` makes ONE id the
owner tenant; a second allowlisted id is a hashed tenant and the owner gate
refuses it).

* :func:`scoped_help` — a non-owner DM reads the lifecycle rows only: the verbs
  ``_handle_command`` lets him run on his own chat (``/cancel`` and the chat
  verbs, through ``_lifecycle_permitted``) and ``/help``. The owner, and every
  room (``room_help`` owns that answer), are unchanged.
* :func:`menu_chat_ids` — the one chat the "/" menu is published to.

The owner test is the one the owner gate uses (``core.instance.is_owner_uid``
with ``allow_local=False``), so help and the gate can never disagree.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The lifecycle verbs a non-owner runs on his own DM chat, in render order.
#: ``/task`` is left out: it is the old form of ``/new``.
LIFECYCLE_VERBS = ("/new", "/chats", "/chat", "/rename", "/close", "/cancel", "/help")


def _is_owner(user_id: Any) -> bool:
    from core.instance import is_owner_uid
    return is_owner_uid(user_id, allow_local=False)


def lifecycle_help(name: str) -> str:
    """The help a non-owner reads: one line per lifecycle verb, from the same rows
    as the owner's ``/help`` (the verb table and the seat's local rows)."""
    from core.verbs import verb_for
    from surfaces.telegram import harness as h
    local = {row[0]: row for rows in h._LOCAL_ROWS.values() for row in rows}
    lines = []
    for verb in LIFECYCLE_VERBS:
        if verb in local:
            _n, usage, text = local[verb]
        else:
            row = verb_for(verb)
            if row is None:
                continue
            usage, text = h._USAGE.get(verb, ""), row.help
        lines.append(h._help_line(verb, usage, text))
    return (f"Commands you can use here:\n" + "\n".join(lines)
            + f"\n\nOr just send a message to talk to {name}.")


def scoped_help(result: Any) -> Optional[str]:
    """The ``/help`` of a non-owner DM sender, or None (the caller's help stands).

    Fail-closed to the short form: a fault in the owner test reads as not the
    owner, so a stranger never reads the catalog because a check raised."""
    identity = result.inbound.identity
    if getattr(identity.source, "chat_type", "dm") != "dm":
        return None
    if _is_owner(identity.user_id):
        return None
    args = (getattr(result.inbound, "text", "") or "").split()[1:]
    if args and "/" + args[0].lstrip("/").lower() in LIFECYCLE_VERBS:
        return None   # `/help close`: the one verb's detail, which he may run
    from surfaces.telegram.harness import _agent_name
    return lifecycle_help(_agent_name())


def menu_chat_ids() -> list:
    """The chats the "/" menu is published to: the owner's own Telegram chat only.

    ``core.instance.resolve_owner_telegram_id`` names it (an explicit
    ``POLYROB_OWNER_TELEGRAM_ID``, or a single-entry allowlist). It is the id
    ``owner_surface_alias`` makes the owner tenant, so it is the only Telegram id
    whose verbs run; with no such id nobody gets a menu (the broad scopes are
    still cleared)."""
    try:
        from core.instance import owner_surface_alias, resolve_owner_telegram_id
        owner = resolve_owner_telegram_id()
        if owner and owner.isdigit() and owner_surface_alias(owner, "telegram"):
            return [int(owner)]
    except Exception:
        logger.warning("telegram menu: owner id unresolved (no menu published)",
                       exc_info=True)
    return []
