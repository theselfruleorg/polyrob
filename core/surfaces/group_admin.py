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

from core.runtime_paths import surfaces_db_path
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
    return surfaces_db_path(_data_dir(container))


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


def _ledger_for_read(container: Any):
    """The room log for a READ, or ``None`` when there is nothing to read.

    ⚠️ D58: a READ never CREATES a store. ``GroupLedger.__init__`` runs its
    DDL, so `/groups tail` on a box that has never logged a room line minted an
    empty ``surfaces.db`` and then reported "No ledger rows" — indistinguishable
    from a room that was genuinely silent, and a file the owner did not ask for.
    A registered service is already open and is returned as-is.
    """
    svc = _service(container, "group_ledger")
    if svc is not None:
        return svc
    path = _surfaces_db(container)
    if not os.path.isfile(path):
        return None
    from core.surfaces.group_ledger import GroupLedger
    return GroupLedger(path)


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
    """The sender's per-chat role for ``(surface, chat_id)`` — read-only.

    The seam a seat needs when it has NOT gone through the routing boundary
    for THIS room (a DM using the explicit ``<surface> <chat_id>`` grammar:
    ``identity.chat_role`` is only ever stamped for a message that arrived
    THROUGH that room — 044 T18 fix round 1, Critical 1b).

    ⚠️ D3: fail-CLOSED to ``blocked``, mirroring ``GroupRoles.role``'s own
    contract, which this delegates to. ``blocked`` is the room's only
    per-member deny and it lives in the very store this read consults, so
    answering ``member`` on a fault un-blocked everyone an owner had blocked.
    """
    try:
        return _roles(container).role(surface, _norm_chat_id(chat_id), str(raw_user_id),
                                      is_owner=False)
    except Exception as e:
        logger.warning("group_admin: room_role lookup failed for %s:%s (%s) — "
                       "reading as blocked; a deny must survive a read fault",
                       surface, chat_id, e)
        return "blocked"


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
        from core.surfaces.room_label import render_room_line
        for r in active:
            surface, chat_id = r["surface"], r["chat_id"]
            pol = chat_policy.load(home_dir, owner_uid, surface, chat_id)
            # 057 WS-D: the NAME is `chat.name` (or surface:chat_id); the owner's
            # allowlist note renders as a dated LABEL after it, never as the
            # room's title. `polyrob owner groups allow` calls that field "Label"
            # — three renderers promoted it to a name the room never had.
            lines.append("• " + render_room_line(r, pol, f"mode={pol.mode}"))
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

def _grant_refusal(key: str, value: Any, surface: str = "telegram") -> Optional[str]:
    """Every name in a grant list must be real, so a typo is said at the seat and not
    silently dropped at read (the read intersects too: a hand-edited file never widens)."""
    from core.surfaces.room_grants import ADMIN_BUNDLES, ADMIN_SURFACES, member_grantable
    if key == "chat.room_tools":
        allowed, what = member_grantable(), "tools a room may be given"
    elif key == "chat.admin_tools":
        allowed, what = frozenset(ADMIN_BUNDLES), "admin bundles"
    else:
        return None
    from core.prefs import validate_pref
    ok, coerced, err = validate_pref(key, value)
    if not ok:
        return err
    if key == "chat.admin_tools" and coerced and surface not in ADMIN_SURFACES:
        return f"/admin runs on Telegram only, so {surface} cannot hold an /admin bundle"
    bad = [v for v in coerced if v not in allowed]
    if bad:
        return (f"{key}: {', '.join(map(str, bad))} is not in the {what}: "
                f"{', '.join(sorted(allowed))}")
    return None


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
    # switching a room priced at 75,000 TOKEN (~$5) to USDC would have quoted the
    # next payer 75,000 USDC. Binding here means the owner never types an asset
    # id and a bare value can never reach the store.
    if key.endswith("_units") and str(value).strip():
        value = _bind_unit_price(home_dir, owner_uid, surface, chat_id, value)
    if str(value).strip().lower() in ("unset", "-"):
        ok, msg = chat_policy.unset(home_dir, owner_uid, surface, chat_id, key)
        if not ok:
            return f"❌ {msg}"
        return f"✅ {key} unset for {label} (back to default)."
    refused = _grant_refusal(key, value, surface)
    if refused:
        return f"❌ {refused}"
    ok, msg = chat_policy.set(home_dir, owner_uid, surface, chat_id, key, value)
    if not ok:
        return f"❌ {msg}"
    return f"✅ {key} = {value!r} for {label}."


def show_tools(container: Any, owner_uid: str, surface: str, chat_id: str) -> str:
    """What this room holds beyond its base toolset, and what it could hold."""
    chat_id = _norm_chat_id(chat_id)
    from core.surfaces import chat_policy, room_grants
    pol = chat_policy.load(_data_dir(container), owner_uid, surface, chat_id)
    from core.surfaces.room_policy import room_tool_ids
    grantable = sorted(room_grants.member_grantable())
    if getattr(pol, "unreadable", False):
        return (f"The settings file of {surface}:{chat_id} could not be read, so its tools "
                "are UNKNOWN and every /admin bundle refuses. Repair or remove the file.")
    admin_line = (
        f"• /admin bundles: {', '.join(room_grants.live_admin_bundles(pol)) or 'none, so /admin is off'}"
        f" (available: {', '.join(sorted(room_grants.ADMIN_BUNDLES))})"
        if surface in room_grants.ADMIN_SURFACES else "• /admin: runs on Telegram only")
    return "\n".join((
        f"Tools in {surface}:{chat_id}:",
        f"• every turn: {', '.join(room_tool_ids(policy=pol)) or 'none'}",
        f"• room tools you added: {', '.join(room_grants.live_room_tools(pol)) or 'none'}"
        f" (grantable: {', '.join(grantable)})",
        admin_line,
        f"• /admin turns per day: {pol.admin_turns_per_day}",
    ))


def set_tools(container: Any, owner_uid: str, surface: str, chat_id: str, *,
              admin: bool, add: bool, name: str) -> str:
    """Add or remove one name in ``chat.room_tools`` / ``chat.admin_tools``."""
    chat_id = _norm_chat_id(chat_id)
    from core.surfaces import chat_policy
    field = "admin_tools" if admin else "room_tools"
    current = list(getattr(chat_policy.load(_data_dir(container), owner_uid, surface,
                                            chat_id), field) or ())
    name = str(name).strip().lower()
    if add and name in current or not add and name not in current:
        return f"{name} is {'already' if add else 'not'} in chat.{field} for {surface}:{chat_id}."
    wanted = current + [name] if add else [v for v in current if v != name]
    if not wanted:
        return set_key(container, owner_uid, surface, chat_id, f"chat.{field}", "-")
    return set_key(container, owner_uid, surface, chat_id, f"chat.{field}", ",".join(wanted))


def room_ref(surface: str, chat_id: Any) -> str:
    """``<surface> <chat_id>`` as a seat line names a room from anywhere: a negative
    Telegram id takes the ``_`` spelling, the one both grammars read."""
    chat = str(chat_id)
    return f"{surface} _{chat[1:]}" if chat.startswith("-") else f"{surface} {chat}"


def _is_platform_id(ref: str, surface: str = "telegram") -> bool:
    """A name never writes a role (101 R1); the id a surface sends for an account may."""
    if ref.isascii() and ref.isdigit():
        return True
    from core.surfaces.catalog import get as _surface_row
    row = _surface_row(surface)
    return bool(row and row.account_id_shape and ref.isascii()
                and re.fullmatch(row.account_id_shape, ref))


def _name_answer(container: Any, surface: str, chat_id: str, ref: str, role: str) -> str:
    """A name found in the room log: its uid and the exact line. Writes nothing.

    ⚠️ 101 R1: ``sender_name`` is a first name when a member has no username, so
    a member who renames himself to ``@bob`` would receive the owner's grant to
    ``@bob``. The id comes back to a person, who sends it.
    """
    from core.surfaces.room_targets import resolve_target
    ledger = _ledger_for_read(container)
    rows = []
    if ledger is not None:
        try:
            rows = ledger.tail(surface, chat_id, limit=2000)
        except Exception as e:
            logger.warning("group_admin: name lookup failed for %s (%s)", chat_id, e)
            return (f"❌ I could not read the room log for {ref!r} ({type(e).__name__}). "
                    "Use their platform id, or reply to their message.")
    uid, found = resolve_target(rows, ref)
    if uid is None:
        why = found if "more than one" in str(found) else f"I found no {ref!r} in the room log."
        return f"❌ A role needs a platform id or a reply to their message. {why}"
    return (f"A name is self-chosen, so it does not set a role. {found} is uid {uid} in "
            f"the room log. If that is the right person, send: /groups role "
            f"{room_ref(surface, chat_id)} {uid} {role}")


def set_role(container: Any, surface: str, chat_id: str, user_ref: str, role: str, *,
            by: str) -> str:
    """Grant a per-chat role (admin/blocked), or `member`, which removes the row.

    An unknown role echoes the vocabulary ``GroupRoles.grant`` raises rather than
    writing a row nothing reads. ``user_ref`` that is not a raw numeric platform
    id writes nothing: see :func:`_name_answer`.
    """
    chat_id = _norm_chat_id(chat_id)
    ref = str(user_ref).strip()
    role_norm = str(role).strip().lower()
    if not _is_platform_id(ref, surface):
        return _name_answer(container, surface, chat_id, ref, role_norm)
    label = f"{surface}:{chat_id}"
    if role_norm == "member":
        if _roles(container).revoke(surface, chat_id, ref):
            return f"✅ {ref} is a plain member again in {label}."
        return f"{ref} held no role in {label}, so nothing changed."
    try:
        _roles(container).grant(surface, chat_id, ref, role_norm, granted_by=by)
    except ValueError as e:
        return f"❌ {e}"
    return f"✅ {ref} is now {role_norm} in {label} (granted by {by})."


def list_roles(container: Any, surface: str, chat_id: str) -> str:
    """POLYROB's own role rows for one room. Unreadable is not empty."""
    chat_id = _norm_chat_id(chat_id)
    label = f"{surface}:{chat_id}"
    if _service(container, "group_roles") is None and not os.path.isfile(_surfaces_db(container)):
        return f"No role is set in {label}: everyone but the owner is a member."
    try:
        rows = _roles(container).list(surface, chat_id)
    except Exception as e:
        logger.warning("group_admin: role list failed for %s: %s", label, e)
        return f"The roles of {label} are UNKNOWN: the store could not be read ({type(e).__name__})."
    if not rows:
        return f"No role is set in {label}: everyone but the owner is a member."
    lines = [f"{len(rows)} role(s) in {label}:"]
    for r in rows:
        when = time.strftime("%Y-%m-%d", time.gmtime(float(r.get("created_at") or 0)))
        lines.append(f"• {r['user_id']} — {r['role']} (by {r.get('granted_by') or '?'}, {when})")
    return "\n".join(lines)


def audit(container: Any, surface: str, chat_id: str, n: int = 30) -> str:
    """Who ran which admin tool in this room. A read, never a push: a room never
    writes the owner thread. Rows live as long as ``room_events`` (7 days)."""
    chat_id = _norm_chat_id(chat_id)
    label = f"{surface}:{chat_id}"
    if _service(container, "room_caps") is None and not os.path.isfile(_surfaces_db(container)):
        return f"No admin tool has run in {label}."
    try:
        rows = _room_caps(container).admin_tool_rows(surface, chat_id, limit=max(1, int(n)))
    except Exception as e:
        logger.warning("group_admin: audit read failed for %s: %s", label, e)
        return f"The audit of {label} is UNKNOWN: the store could not be read ({type(e).__name__})."
    if not rows:
        return f"No admin tool has run in {label} in the last 7 days."
    lines = [f"Last {len(rows)} admin tool call(s) in {label}:"]
    for ts, action, uid in rows:
        lines.append(f"[{time.strftime('%m-%d %H:%M UTC', time.gmtime(ts))}] {uid}: {action}")
    return "\n".join(lines)


def tail(container: Any, surface: str, chat_id: str, n: int) -> str:
    """The last ``n`` ledger lines for this room, attributed."""
    chat_id = _norm_chat_id(chat_id)
    label = f"{surface}:{chat_id}"
    ledger = _ledger_for_read(container)
    if ledger is None:
        return (f"No room log on this box yet — nothing has been recorded for "
                f"{label} or any other room.")
    try:
        rows = ledger.tail(surface, chat_id, limit=max(1, int(n)))
    except Exception as e:
        logger.warning("group_admin: ledger tail failed for %s: %s", label, e)
        return (f"Room log for {label}: unavailable ({type(e).__name__}: "
                f"{str(e)[:100]}) — this is UNKNOWN, not an empty room.")
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

__all__ = ["allow_here", "audit", "data_home", "deny_here", "is_room_allowed", "list_roles",
          "list_rooms", "mute", "normalize_chat_id", "room_ref", "room_role", "set_key",
          "set_mode", "set_role", "set_tools", "show_tools", "tail"]
