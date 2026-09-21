"""WS-A access-tier resolver — classify one inbound's sender into a trust tier.

Four tiers, resolved ONCE at the routing boundary and carried into the dispatcher:

- **OWNER** — the bound owner principal, the local single-user operator, or a paired
  user. May command the agent (steering turn).
- **CORRESPONDENT** — a third party the agent INITIATED contact with, i.e. an ACTIVE
  binding in the correspondent registry. Their inbound is DATA delivered only to the
  originating session; it can never command the agent.
- **GROUP_MEMBER** (044) — a non-owner sender inside an allowlisted room
  (``GROUP_CHAT_ENABLED`` + ``core/surfaces/group_allowlist.py``). Rooms: allowlisted
  chat + per-chat role; tool power bounded by the audience
  (``core/surfaces/room_policy.py``) — never thread membership, never the correspondent
  DATA rail. See ``docs/guide/groups.md``.
- **DENIED** — anonymous, unknown, ``blocked`` in a room, or anything that isn't
  clearly one of the above.

Invariants:
- **Tier = authenticated sender**, never thread membership (the registry keys on the
  sender address; ``thread_id`` only disambiguates among that sender's own sessions).
- **Fail-closed on the CORRESPONDENT→OWNER boundary**: any fault degrades toward
  DENIED and never UPGRADES a sender to OWNER.
- **Groups/channels** are DENIED unless the operator opted in (``GROUP_CHAT_ENABLED``)
  AND the chat is allowlisted. Inside an allowed room the sender carries a per-chat
  ROLE (044 T16, ``core/surfaces/group_roles.py``): the owner principal is OWNER,
  ``blocked`` is DENIED, and admin/member are both ``GROUP_MEMBER``.

Pure decision function; no surface/transport imports. Reads ``POLYROB_LOCAL`` directly
(like ``core/pairing``) to stay on the core side of the core→agents boundary.
"""
from __future__ import annotations

import logging
import os
from enum import Enum
from core.config_policy import _mode_capability_default
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

from core.env import bool_from as _bool_from

# Single-user local mode (`is_owner(local=True)`) grants OWNER to any non-empty uid.
# That is correct ONLY for surfaces whose principal is the local operator — NEVER for a
# network surface where the sender id is a forgeable remote address (Fusion CRITICAL:
# else any email/telegram sender becomes an owner command-turn under POLYROB_LOCAL).
_LOCAL_OWNER_SURFACES = {"cli", "local", "repl"}

#: Network surfaces whose SENDER ADDRESS is forgeable, so no ownership evidence
#: that rests on that address may be honoured. The ONE definition — the
#: dispatcher imports it from here (it used to keep a private copy, and the two
#: guarded different halves of the same rule).
#:
#: ⚠️ Owner-by-email is OFF in v1 because a ``From:`` header is trivially
#: spoofed. A PAIRING row is keyed on the same forgeable address, so honouring
#: it here would re-open exactly the hole the tier model closes: anyone who can
#: forge a paired address would reach the obey-path as OWNER. Such a surface is
#: correspondent-or-denied by construction.
FORGEABLE_NETWORK_SURFACES = frozenset({"email"})


class AccessTier(str, Enum):
    OWNER = "owner"
    CORRESPONDENT = "correspondent"
    GROUP_MEMBER = "group_member"  # W3: non-owner in an allowlisted group
    #: 044 T16 renamed GROUP_PARTICIPANT -> GROUP_MEMBER (the room vocabulary is
    #: owner/admin/member/blocked, and "participant" named none of them). Same
    #: VALUE, so Enum makes this an alias: `AccessTier.GROUP_PARTICIPANT is
    #: AccessTier.GROUP_MEMBER`. Kept for one release so an out-of-tree caller
    #: does not break; new code uses GROUP_MEMBER.
    GROUP_PARTICIPANT = "group_member"
    DENIED = "denied"


def _is_owner_or_paired(container: Any, uid: str, env: Mapping[str, str],
                        *, allow_local: bool, allow_pairing: bool = True) -> bool:
    """True if ``uid`` is the owner/local operator or a paired user. Fail-closed.

    ``allow_local`` gates the single-user local-owner bypass: it is only honoured for a
    trusted local surface (never a network surface — see ``_LOCAL_OWNER_SURFACES``).

    ``allow_pairing`` gates the PAIRING branch the same way (D1). A pairing row
    is keyed on the sender address, so on a forgeable-address surface
    (:data:`FORGEABLE_NETWORK_SURFACES`) it is not evidence of anything: the
    dispatcher's own forgeable-sender refusal only guarded the legacy path taken
    with the correspondent model OFF, so with the model ON a paired address
    became OWNER on email and reached the obey-path.

    ⚠️ That surface condition is INERT on an UNBOUND install: the owner principal is
    then the local tenant itself, so a uid of ``local`` is owner by PRINCIPAL on any
    surface and never reaches the ``local`` branch. It is not a hole — a network
    sender is hashed to a ``u_…`` id and can never present ``local`` — but the
    scoping stops doing work until an owner is bound. See
    ``core.instance.is_owner_local_safe`` for the same caveat.
    """
    try:
        from core.instance import is_owner, resolve_owner_principal
        # Repo-SSOT falsey-set parse — keeps this OWNER gate in agreement with
        # core.config_policy.local_mode_enabled (the old private opt-in truth
        # set disagreed on values like "enabled": policy said local, this said not).
        local = allow_local and _bool_from(env, "POLYROB_LOCAL", False)
        if is_owner(uid, owner_principal=resolve_owner_principal(env), local=local):
            return True
    except Exception as e:  # never let an owner-check fault grant or crash
        logger.debug("access-tier owner check failed (fail-closed): %s", e)
    if not allow_pairing:
        return False
    try:
        from core.pairing import PairingStore
        cfg = getattr(container, "config", None) if container else None
        from core.runtime_paths import data_dir_or_home
        data_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
        store = PairingStore(os.path.join(data_dir, "pairing.db"))
        if store.is_paired(uid):
            return True
    except Exception as e:
        logger.debug("access-tier pairing check failed (fail-closed): %s", e)
    return False


def _group_chat_mode_default() -> bool:
    """Default for GROUP_CHAT_ENABLED when it is unset in the resolved env source:
    ON under effective autonomous mode (core.config_policy._mode_capability_default),
    else OFF — same guarded pattern as core/config.py's MCP consumer seam
    (013 T2 review fix, Finding 1). Without this, `resolve_access_tier` re-derived the
    flag from raw env and disagreed with `SurfaceConfig.group_chat_enabled()` (which the
    dispatcher already checks before reaching here), leaving group ingress DENIED under
    autonomous mode with the env unset. Fail-closed: any import/call fault -> False."""
    try:
        return _mode_capability_default("GROUP_CHAT_ENABLED")
    except Exception as e:
        logger.debug("group-chat mode-default check failed (fail-closed to off): %s", e)
        return False


def _is_owner_principal(uid: str, env: Mapping[str, str]) -> bool:
    """True only for the BOUND owner principal — no pairing, no local bypass.

    The room half of the owner check (044 T16 fix round 1). Fail-closed: any
    fault reads as not-the-owner.
    """
    try:
        from core.instance import is_owner, resolve_owner_principal
        return bool(is_owner(uid, owner_principal=resolve_owner_principal(env),
                             local=False))
    except Exception as e:  # never let an owner-check fault grant or crash
        logger.debug("room owner check failed (fail-closed): %s", e)
        return False


def is_room_owner(uid: Any, env: Optional[Mapping[str, str]] = None) -> bool:
    """PUBLIC probe for "is this the bound owner principal", room rules.

    The same answer :func:`resolve_access_tier` uses inside a room — no pairing,
    no local bypass, fail-closed. Exported because the dispatcher must ask it
    BEFORE the allowlist for one narrow case (044 C6: the owner's `/groups allow
    here` in a room that is not yet allowlisted), and reaching for the private
    name across modules is how two owner checks start disagreeing.
    """
    return _is_owner_principal(str(uid or ""), os.environ if env is None else env)


#: Rooms whose role store could not be read, warned about once per process.
#: An unreadable roles table denies every non-owner in that room, and the
#: operator can only fix that once — so say it once, loudly, not per message.
_ROLE_FAULT_WARNED: set = set()


def _chat_role(container: Any, surface: str, chat_id: str, member_id: str) -> str:
    """The speaker's per-chat role (044 T16). Fail-CLOSED to ``blocked``.

    Prefers the container's registered ``group_roles`` service (installed beside
    the rest of the surface bus in ``core/surfaces/bootstrap.py``) and falls back
    to opening ``surfaces.db`` directly, because ``resolve_access_tier`` is also
    called from seats that never installed the bus.

    ⚠️ D3: this used to answer ``member`` on any fault, which was called
    "least privilege" — it is not. ``blocked`` is the room's ONLY per-member
    deny, and it lives in the very store this read consults, so an unreadable
    store turned every blocked member back into an ordinary one. The deny a room
    owner set must survive a read fault; a member losing one turn to a warning
    does not. The owner principal never reaches here (he is resolved before any
    row is read), so a fault can never lock the owner out of his own room.
    """
    try:
        store = container.get_service("group_roles") if container else None
        if store is None:
            from core.runtime_paths import data_dir_or_home
            from core.surfaces.group_roles import GroupRoles
            cfg = getattr(container, "config", None) if container else None
            data_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
            store = GroupRoles(os.path.join(data_dir, "surfaces.db"))
        return store.role(surface, chat_id, member_id, is_owner=False)
    except Exception as e:
        key = f"{surface}:{chat_id}"
        if key not in _ROLE_FAULT_WARNED:
            _ROLE_FAULT_WARNED.add(key)
            logger.warning(
                "room %s: the per-chat role store is unreadable (%s) — every "
                "non-owner line there is DENIED until it can be read. A "
                "`blocked` member must not be un-blocked by a read fault.",
                key, e)
        return "blocked"


def _stamp_role(identity: Any, role: str) -> None:
    """Carry the resolved role OUT on the envelope so every downstream reader
    (the harness, the ledger, the room turn) uses the SAME answer instead of
    re-deriving a second one. Never raises — a role that cannot be stamped costs
    a label, never a routing decision."""
    try:
        identity.chat_role = role
    except Exception:  # pragma: no cover - a frozen/exotic identity shape
        logger.debug("chat_role could not be stamped on the identity")


def _group_chat_allowed(container: Any, surface: str, chat_id: str) -> bool:
    """Default-DENY group allowlist check. Fail-closed: any fault -> False."""
    try:
        from core.surfaces.group_allowlist import GroupAllowlist
        cfg = getattr(container, "config", None) if container else None
        from core.runtime_paths import data_dir_or_home
        data_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
        store = GroupAllowlist(os.path.join(data_dir, "group_allowlist.db"))
        return store.is_allowed(surface, chat_id)
    except Exception as e:
        logger.debug("group allowlist check failed (fail-closed): %s", e)
        return False


def resolve_access_tier(
    container: Any,
    identity: Any,
    *,
    thread_id: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> AccessTier:
    """Classify ``identity`` into an :class:`AccessTier`. Never raises."""
    src = os.environ if env is None else env
    try:
        uid = (str(getattr(identity, "user_id", "")) or "").strip()
        if not uid:
            return AccessTier.DENIED

        source = getattr(identity, "source", None)
        surface = getattr(source, "surface_id", "") or ""
        chat_type = getattr(source, "chat_type", "dm") or "dm"

        # Multi-party chats: DENIED unless the operator opted into group chat
        # (GROUP_CHAT_ENABLED, W3). When on: the chat must be allowlisted
        # (default-DENY GroupAllowlist), the owner keeps OWNER, and everyone
        # else in an allowed chat becomes GROUP_MEMBER (their messages are
        # mention-gated DATA at the dispatcher — never a command/steer turn).
        if chat_type != "dm":
            raw_group = src.get("GROUP_CHAT_ENABLED", "")
            if raw_group is None or not str(raw_group).strip():
                # Unset -> the mode-governed default (explicit env, even falsey,
                # always wins over the default; only the unset case moves).
                group_on = _group_chat_mode_default()
            else:
                from core.env import parse_bool
                group_on = parse_bool(raw_group, False)
            if not group_on:
                return AccessTier.DENIED
            chat_id = str(getattr(source, "chat_id", "") or "")
            if not chat_id or not _group_chat_allowed(container, surface, chat_id):
                return AccessTier.DENIED
            # Inside a group the owner is the bound PRINCIPAL and nobody else.
            # The local-owner bypass is never honoured (a group sender is a
            # network principal even on a locally-launched surface), and
            # neither is PAIRING: a pairing row says "this person may talk to
            # the agent", which is a DM-scoped grant. Reading it as room
            # ownership would hand any paired user the steer frame, media
            # absorption, the lifecycle verbs and immunity to `blocked` in
            # every allowlisted room (044 T16 fix round 1). A paired non-owner
            # falls through and reads `member` unless a row grants `admin`.
            if _is_owner_principal(uid, src):
                _stamp_role(identity, "owner")
                return AccessTier.OWNER
            # 044 T16: everyone else carries a per-chat ROLE. `blocked` is the
            # only per-member deny; admin and member are both GROUP_MEMBER at the
            # tier level (what separates them is whether their line STEERS, which
            # the harness decides from the stamped role).
            role = _chat_role(container, surface, chat_id,
                              getattr(identity, "raw_user_id", None) or uid)
            _stamp_role(identity, role)
            if role == "blocked":
                return AccessTier.DENIED
            return AccessTier.GROUP_MEMBER

        if _is_owner_or_paired(
                container, uid, src,
                allow_local=surface in _LOCAL_OWNER_SURFACES,
                allow_pairing=surface not in FORGEABLE_NETWORK_SURFACES):
            return AccessTier.OWNER

        # Non-owner: routable ONLY as a known correspondent (active binding).
        registry = container.get_service("correspondent_registry") if container else None
        if registry is not None:
            address = getattr(identity, "raw_user_id", None) or uid
            try:
                row = registry.resolve(surface=surface, address=address, thread_id=thread_id)
            except Exception as e:  # fail-closed: a registry fault is NOT an upgrade
                logger.debug("access-tier registry resolve failed (fail-closed): %s", e)
                row = None
            if row is not None:
                return AccessTier.CORRESPONDENT

        return AccessTier.DENIED
    except Exception as e:  # absolute fail-closed backstop
        logger.debug("resolve_access_tier fault (fail-closed to DENIED): %s", e)
        return AccessTier.DENIED


__all__ = ["AccessTier", "FORGEABLE_NETWORK_SURFACES", "is_room_owner",
           "resolve_access_tier"]
