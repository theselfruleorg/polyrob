"""One resolver from a typed room target to a member of the room log (101).

Pure core. ⚠️ A ledger name is self-chosen when the member has no username, so it
may point at a line and it may never grant authority: a role is written from a
numeric id or a reply (``group_admin.set_role``).
"""
from __future__ import annotations


def scoped_rooms(controller, rooms):
    """The rooms a session's room verbs may see: an admin session's own, else all.
    ``room_matches`` also reads a title fragment, so without this a chat id inside
    another room's title returned the list of every room (101 R8)."""
    scope = getattr(controller, "_room_scope", None)
    here = scope() if callable(scope) else None
    if not here:
        return rooms
    return [r for r in rooms if (r["surface"], str(r["chat_id"])) == (here[0], str(here[1]))]


def resolve_target(rows, target: str, *, allow_unseen: bool = False):
    """``(user_id, display_name)`` or ``(None, reason)`` from the room ledger.

    Matches the sender id exactly, or the sender name with or without ``@``,
    case-insensitive. A bare number with no ledger row is taken as a user id.
    Ambiguity refuses — a ban must never land on the nearest name.
    """
    want = (target or "").strip()
    if not want:
        return None, "name the target (user id or @name from room_read)."
    bare = want.lstrip("@").lower()
    hits = {}
    for r in rows:
        # Only the agent's OWN rows are skipped. Another bot (a spam bot) is a
        # valid target; the live-status probe still protects an admin bot.
        if not r.sender_id or r.sender_id == "agent":
            continue
        name = (r.sender_name or "").strip()
        if want == r.sender_id or (name and name.lstrip("@").lower() == bare):
            hits[r.sender_id] = name or r.sender_id
    if len(hits) == 1:
        uid, name = next(iter(hits.items()))
        return uid, name
    if len(hits) > 1:
        listed = ", ".join(f"{n} (uid={u})" for u, n in hits.items())
        return None, f"'{want}' matches more than one member: {listed}. Use the uid."
    # ⚠️ A uid with no ledger row is accepted ONLY to LIFT a mute/ban (the
    # member may have aged out of the ledger). A mute/ban/delete must name
    # someone who actually posted here, so a planted line cannot aim one at an
    # arbitrary account.
    if allow_unseen and bare.lstrip("-").isdigit():
        return want.lstrip("@"), want.lstrip("@")
    return None, (f"no member named '{want}' in this room's ledger. Read the "
                  f"room with room_read and pass the uid.")
