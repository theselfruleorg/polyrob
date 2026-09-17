"""044 T18: the ONE owner-admin helper set for group presence.

Every seat that lets an owner (or a room's own admin) manage a group chat —
Telegram ``/groups``/``/mute``, ``polyrob owner groups …``, the REPL, a future
console panel — renders through this module, never through
``GroupAllowlist``/``GroupRoles``/``GroupLedger``/``chat_policy`` directly.
One function, one verified sentence back. Mirrors ``core/surfaces/owner_admin.py``.

Pure core: no import from ``agents``/``surfaces``/``tools`` at module top.

Each function takes a ``container`` — anything exposing ``.config.data_dir``
and ``.get_service(name)`` (a real DI container, or a small adapter a CLI
command builds). A registered ``group_ledger``/``group_roles`` service is
reused when present (the normal server/Telegram path, installed by
``core/surfaces/bootstrap.py``); otherwise a fresh handle is opened on
``<data_dir>/surfaces.db`` (the CLI path, which never installs the bus).

⚠️ ONE verb is NOT here: ``/groups service`` (044 T20) creates a durable CRON
job, and ``core`` may not import ``cron`` (tier-0 -> tier-4, the layering
ratchet). It lives in ``cron/room_service.py`` instead — still ONE helper both
seats render through, just in the tier that owns the machinery it writes to.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DUR_RE = re.compile(r"^(\d+)\s*([mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _data_dir(container: Any) -> str:
    from core.runtime_paths import container_data_home
    return container_data_home(container)


def data_home(container: Any) -> str:
    """The data home this seat operates in — the PUBLIC name for ``_data_dir``.

    044 T20 fix round 1 (Minor 9): ``cron/room_service.py`` (the `service` verb's
    helper, which cannot live in this module because core may not import cron)
    needs the same resolution, and reaching for a private name across modules is
    how two seats start disagreeing about which data home they mean.
    """
    return _data_dir(container)


def is_room_allowed(container: Any, surface: str, chat_id: Any) -> bool:
    """Is ``(surface, chat_id)`` an ACTIVE allowlisted room? Fail-closed.

    Normalizes the ``_`` CLI alias first, so a seat always asks about the same
    room every other function here writes to.
    """
    return _allowlist(container).is_allowed(surface, _norm_chat_id(chat_id))


def _surfaces_db(container: Any) -> str:
    return os.path.join(_data_dir(container), "surfaces.db")


def _service(container: Any, name: str) -> Optional[Any]:
    try:
        return container.get_service(name) if container is not None else None
    except Exception as e:
        logger.debug("group_admin: get_service(%s) failed: %s", name, e)
        return None


def _allowlist(container: Any):
    from core.surfaces.group_allowlist import GroupAllowlist
    return GroupAllowlist(os.path.join(_data_dir(container), "group_allowlist.db"))


def _ledger(container: Any):
    svc = _service(container, "group_ledger")
    if svc is not None:
        return svc
    from core.surfaces.group_ledger import GroupLedger
    return GroupLedger(_surfaces_db(container))


def _roles(container: Any):
    svc = _service(container, "group_roles")
    if svc is not None:
        return svc
    from core.surfaces.group_roles import GroupRoles
    return GroupRoles(_surfaces_db(container))


def _room_caps(container: Any):
    svc = _service(container, "room_caps")
    if svc is not None:
        return svc
    from core.surfaces.room_caps import RoomCaps
    return RoomCaps(_surfaces_db(container))


def _parse_duration_seconds(spec: str) -> Optional[int]:
    m = _DUR_RE.match(str(spec).strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        return None
    return n * _UNIT_SECONDS[unit]


def _norm_chat_id(chat_id: Any) -> str:
    """044 T18 fix round 1 (Important 5c): the file-safe CLI alias for a real
    (negative) Telegram chat id — ``_1001234567`` for ``-1001234567``. A
    leading ``-`` on a shell argument needs Click's ``--`` separator; a
    leading ``_`` needs nothing at all. Every function below normalizes
    BEFORE it touches a store, so the alias and the real id are always the
    same room. Anything else (a real id, a non-Telegram surface's chat id)
    passes through unchanged."""
    s = str(chat_id)
    if s.startswith("_") and s[1:].isdigit():
        return "-" + s[1:]
    return s


def room_role(container: Any, surface: str, chat_id: str, raw_user_id: str) -> str:
    """The sender's per-chat role for ``(surface, chat_id)`` — read-only,
    fail-open to ``member`` (mirrors ``GroupRoles.role``'s own contract).
    The seam a seat needs when it has NOT gone through the routing boundary
    for THIS room (a DM using the explicit ``<surface> <chat_id>`` grammar:
    ``identity.chat_role`` is only ever stamped for a message that arrived
    THROUGH that room — 044 T18 fix round 1, Critical 1b)."""
    try:
        return _roles(container).role(surface, _norm_chat_id(chat_id), str(raw_user_id),
                                      is_owner=False)
    except Exception as e:
        logger.debug("group_admin: room_role lookup failed (reading as member): %s", e)
        return "member"


def allow_here(container: Any, surface: str, chat_id: str, title: str, *,
              owner_uid: str) -> str:
    """Allowlist ``(surface, chat_id)`` and — when a title is given — name the
    room's ``chat.name``, so the very next ``/groups list`` shows something a
    human recognizes rather than a bare numeric chat id."""
    chat_id = _norm_chat_id(chat_id)
    label = f"{surface}:{chat_id}"
    _allowlist(container).allow(surface, chat_id, note=title or "")
    if title:
        from core.surfaces import chat_policy
        ok, msg = chat_policy.set(_data_dir(container), owner_uid, surface, chat_id,
                                  "chat.name", title)
        if not ok:
            logger.warning("group_admin: allow_here could not set chat.name for %s: %s",
                           label, msg)
    suffix = f' "{title}"' if title else ""
    return f"✅ Allowed {label}{suffix}."


def deny_here(container: Any, surface: str, chat_id: str, *,
             owner_uid: Optional[str] = None) -> str:
    """Revoke the allowlist row. The ledger is kept until its own retention —
    denying a room is not a request to forget it happened."""
    chat_id = _norm_chat_id(chat_id)
    label = f"{surface}:{chat_id}"
    if _allowlist(container).revoke(surface, chat_id):
        return f"✅ Denied {label}. (ledger kept until retention)"
    return f"{label} was not active."


def list_rooms(container: Any, owner_uid: str) -> str:
    """Every allowlisted room: its name, mode, and status.

    044 T21: a room the bot was KICKED from is named too. Its row is ``left``,
    not ``active``, so filtering to active alone made it vanish from the seat —
    the owner would see a room go quiet and have nothing to read it against.
    ``/groups allow`` restores it after a rejoin.
    """
    from core.surfaces import chat_policy
    rows = _allowlist(container).list_all()
    active = [r for r in rows if r.get("status") == "active"]
    left = [r for r in rows if r.get("status") == "left"]
    home_dir = _data_dir(container)
    lines = []
    if active:
        lines.append(f"{len(active)} room(s):")
        for r in active:
            surface, chat_id = r["surface"], r["chat_id"]
            pol = chat_policy.load(home_dir, owner_uid, surface, chat_id)
            name = pol.name or r.get("note") or chat_id
            lines.append(f"• {surface}:{chat_id} \"{name}\" — mode={pol.mode}")
    elif not left:
        return "No group chats allowed (default-DENY)."
    for r in left:
        lines.append(f"• {r['surface']}:{r['chat_id']} — LEFT (the bot was removed; "
                     f"`/groups allow {r['surface']} {r['chat_id']}` after a rejoin)")
    return "\n".join(lines)


def set_mode(container: Any, owner_uid: str, surface: str, chat_id: str, mode: str) -> str:
    """Set ``chat.mode`` (mention/active/listen/off). A bad value comes back
    with the valid vocabulary (from ``core.prefs.validate_pref``'s own enum
    error), never a bare rejection."""
    chat_id = _norm_chat_id(chat_id)
    from core.surfaces import chat_policy
    ok, msg = chat_policy.set(_data_dir(container), owner_uid, surface, chat_id,
                              "chat.mode", mode)
    if not ok:
        return f"❌ {msg}"
    return f"✅ {surface}:{chat_id} mode -> {mode}"



def _bind_unit_price(home_dir, owner_uid, surface, chat_id, value) -> str:
    """Append the room's CURRENT asset to a bare unit price.

    An explicit ``"<amount> <asset_id>"`` is passed through untouched, so an
    owner can deliberately pre-price a room for an asset it has not switched to
    yet. A value we cannot bind (no asset configured) is returned unchanged and
    refuses at READ time, naming the remedy — never silently reinterpreted.
    """
    text = str(value).strip()
    if len(text.split()) != 1:
        return text
    try:
        from core.surfaces import chat_policy
        policy = chat_policy.load(home_dir, owner_uid, surface, chat_id)
        asset_id = str(getattr(policy, "paid_asset", "") or "").strip().lower()
    except Exception as e:
        logger.debug("unit price: room asset unresolved (%s)", e)
        return text
    return f"{text} {asset_id}" if asset_id else text

def set_key(container: Any, owner_uid: str, surface: str, chat_id: str,
           key: str, value: Any) -> str:
    """Set (or, with ``value`` = ``unset``/``-``, clear) one ``chat.*`` key."""
    chat_id = _norm_chat_id(chat_id)
    from core.surfaces import chat_policy
    home_dir = _data_dir(container)
    label = f"{surface}:{chat_id}"
    # ⚠️ The guide documents `/groups set here paid_ban_max_duration 6h` while
    # `chat_policy.set` refuses any key not starting with `chat.` — so the
    # documented line was refused, verbatim, in prod (2026-09-15). A room takes
    # `chat.*` settings and nothing else, so a bare key is unambiguous: prefix
    # it. An unknown key is still refused by `validate_pref` below, which is the
    # check that matters — this only removes a prefix nobody can omit by choice.
    key = str(key).strip()
    if key and not key.startswith("chat."):
        key = f"chat.{key}"
    # ⚠️ A unit price is BOUND to the asset it was written for. A bare number
    # was reinterpreted against whatever `chat.paid_asset` happened to be, so
    # switching a room priced at 75,000 PNL (~$5) to USDC would have quoted the
    # next payer 75,000 USDC. Binding here means the owner never types an asset
    # id and a bare value can never reach the store.
    if key.endswith("_units") and str(value).strip():
        value = _bind_unit_price(home_dir, owner_uid, surface, chat_id, value)
    if str(value).strip().lower() in ("unset", "-"):
        ok, msg = chat_policy.unset(home_dir, owner_uid, surface, chat_id, key)
        if not ok:
            return f"❌ {msg}"
        return f"✅ {key} unset for {label} (back to default)."
    ok, msg = chat_policy.set(home_dir, owner_uid, surface, chat_id, key, value)
    if not ok:
        return f"❌ {msg}"
    return f"✅ {key} = {value!r} for {label}."


def set_role(container: Any, surface: str, chat_id: str, user_ref: str, role: str, *,
            by: str) -> str:
    """Grant a per-chat role (admin/member/blocked). An unknown role echoes the
    vocabulary ``GroupRoles.grant`` raises rather than writing a row nothing
    reads.

    044 T18 fix round 1 (Important 4): ``user_ref`` MUST be the raw numeric
    platform id — ``GroupRoles`` keys on it, so a grant to ``@somebody`` (or
    any other non-numeric handle, WITH or WITHOUT a leading ``@`` — a ruled
    NO, since ``@somebody`` cannot be resolved to an id either way) would
    write a row nothing ever reads, confirming a permission that can never
    take effect. ``/groups admins here`` (044 T19) is the seam that turns a
    handle into a real id.
    """
    chat_id = _norm_chat_id(chat_id)
    ref = str(user_ref).strip()
    if not ref.isdigit():
        return (f"❌ {user_ref!r} is not a numeric id — a room role is keyed on "
                "the raw platform id, not a handle. See /groups admins here "
                "for real ids.")
    role_norm = str(role).strip().lower()
    try:
        _roles(container).grant(surface, chat_id, ref, role_norm, granted_by=by)
    except ValueError as e:
        return f"❌ {e}"
    return f"✅ {user_ref} is now {role_norm} in {surface}:{chat_id} (granted by {by})."


def tail(container: Any, surface: str, chat_id: str, n: int) -> str:
    """The last ``n`` ledger lines for this room, attributed."""
    chat_id = _norm_chat_id(chat_id)
    rows = _ledger(container).tail(surface, chat_id, limit=max(1, int(n)))
    label = f"{surface}:{chat_id}"
    if not rows:
        return f"No ledger rows for {label}."
    lines = [f"Last {len(rows)} line(s) in {label}:"]
    for r in rows:
        who = r.sender_name or r.sender_id or "?"
        ts = time.strftime("%H:%M UTC", time.gmtime(r.ts))
        text = (r.text or "")[:200]
        lines.append(f"[{ts}] {who} ({r.role_at_write}): {text}")
    return "\n".join(lines)


def mute(container: Any, owner_uid: str, surface: str, chat_id: str, duration: str) -> str:
    """Set ``chat.mute_until`` = now + *duration* (e.g. ``30m``/``2h``/``1d``) —
    the room stays in ``listen`` (owner/admin-addressed only) until it expires."""
    chat_id = _norm_chat_id(chat_id)
    from core.surfaces import chat_policy
    secs = _parse_duration_seconds(duration)
    if secs is None:
        return f"bad duration {duration!r} (use e.g. 30m, 2h, 1d)"
    until = time.time() + secs
    ok, msg = chat_policy.set(_data_dir(container), owner_uid, surface, chat_id,
                              "chat.mute_until", until)
    if not ok:
        return f"❌ {msg}"
    until_str = time.strftime("%H:%M UTC", time.gmtime(until))
    return f"🔇 {surface}:{chat_id} muted until {until_str} ({duration})."


#: Public name for the chat-id normalizer. ``cron/room_service.py`` (044 T20 —
#: the room SERVICE job lives in the cron tier, see below) must resolve the SAME
#: room this module does, alias and all.
normalize_chat_id = _norm_chat_id

__all__ = ["allow_here", "data_home", "deny_here", "is_room_allowed", "list_rooms",
          "mute", "normalize_chat_id", "room_role", "set_key", "set_mode",
          "set_role", "tail"]
