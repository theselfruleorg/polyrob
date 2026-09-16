"""Who, on a given surface, may never be the TARGET of a room action (046 T5).

⚠️ This module exists because `core.instance.is_owner` could not answer the
question and looked like it did. `is_owner(uid)` with no ``owner_principal=`` and
``local=False`` evaluates ``bool("") and uid == ""`` — a PERMANENT ``False``. The
one call site that mattered (`core/surfaces/room_actions._target_protection`)
also passed the RAW Telegram id, while the owner principal is a hashed ``u_…``
id, so even supplying the principal would not have matched. The result was that
a member could buy a moderation action against the OWNER.

The owner is resolved the way the ROUTING tier resolves them — by their address
on that surface — never from the `group_roles` table, which only knows rows we
wrote and normally has none for the owner.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class OwnerUnknown(RuntimeError):
    """This surface has no resolvable owner address.

    ⚠️ Raised, not returned as ``None``-means-nobody. A sale we cannot bound is
    not a sale: the caller must REFUSE, because "I could not find the owner" and
    "the owner is not this person" are different facts and only one of them is
    safe to act on.
    """


def owner_address_for(surface: str) -> str:
    """The owner's raw address on *surface*. Raises :class:`OwnerUnknown`.

    Telegram is the only surface with a room model today; another surface gets
    its row here, never a second resolver at a call site.
    """
    surf = (surface or "").strip().lower()
    if surf == "telegram":
        from core.instance import resolve_owner_telegram_id
        owner = resolve_owner_telegram_id()
        if owner:
            return str(owner)
        raise OwnerUnknown(
            "no owner Telegram id is configured (POLYROB_OWNER_TELEGRAM_ID, or "
            "a single ALLOWED_TELEGRAM_USER_IDS entry)")
    raise OwnerUnknown(f"no owner address resolver for surface {surface!r}")


def is_owner_address(surface: str, address: Optional[str]) -> bool:
    """Is *address* the owner's own address on *surface*?

    Propagates :class:`OwnerUnknown` — the caller decides what an unanswerable
    question means, and for a paid action it means refuse.
    """
    owner = owner_address_for(surface)
    return bool(address) and str(address).strip() == owner


#: Telegram member statuses that are an ADMINISTRATOR of the chat itself.
#:
#: ⚠️ Read live from `getChatMember`, never from our own roles table: a chat
#: admin who was never granted a `group_roles` row is still an admin of that
#: room, and selling an action against them is selling something the bot will
#: usually be refused anyway (Telegram will not restrict an administrator).
CHAT_ADMIN_STATUSES = frozenset({"creator", "administrator"})


__all__ = ["CHAT_ADMIN_STATUSES", "OwnerUnknown", "is_owner_address",
           "owner_address_for"]
