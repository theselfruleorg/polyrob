"""WS-A access-tier resolver — classify one inbound's sender into a trust tier.

Three tiers, resolved ONCE at the routing boundary and carried into the dispatcher:

- **OWNER** — the bound owner principal, the local single-user operator, or a paired
  user. May command the agent (steering turn).
- **CORRESPONDENT** — a third party the agent INITIATED contact with, i.e. an ACTIVE
  binding in the correspondent registry. Their inbound is DATA delivered only to the
  originating session; it can never command the agent.
- **DENIED** — anonymous, unknown, group/multi-party, or anything that isn't clearly
  one of the above.

Invariants:
- **Tier = authenticated sender**, never thread membership (the registry keys on the
  sender address; ``thread_id`` only disambiguates among that sender's own sessions).
- **Fail-closed on the CORRESPONDENT→OWNER boundary**: any fault degrades toward
  DENIED and never UPGRADES a sender to OWNER.
- **Groups/channels are DENIED in v1** — the envelope is single-principal, so there is
  no safe per-author tiering inside a multi-party chat yet.

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

_BOOL_TRUE = {"1", "true", "yes", "on"}

# Single-user local mode (`is_owner(local=True)`) grants OWNER to any non-empty uid.
# That is correct ONLY for surfaces whose principal is the local operator — NEVER for a
# network surface where the sender id is a forgeable remote address (Fusion CRITICAL:
# else any email/telegram sender becomes an owner command-turn under POLYROB_LOCAL).
_LOCAL_OWNER_SURFACES = {"cli", "local", "repl"}


class AccessTier(str, Enum):
    OWNER = "owner"
    CORRESPONDENT = "correspondent"
    GROUP_PARTICIPANT = "group_participant"  # W3: non-owner in an allowlisted group
    DENIED = "denied"


def _is_owner_or_paired(container: Any, uid: str, env: Mapping[str, str],
                        *, allow_local: bool) -> bool:
    """True if ``uid`` is the owner/local operator or a paired user. Fail-closed.

    ``allow_local`` gates the single-user local-owner bypass: it is only honoured for a
    trusted local surface (never a network surface — see ``_LOCAL_OWNER_SURFACES``).
    """
    try:
        from core.instance import is_owner, resolve_owner_principal
        local = allow_local and (env.get("POLYROB_LOCAL", "") or "").strip().lower() in _BOOL_TRUE
        if is_owner(uid, owner_principal=resolve_owner_principal(env), local=local):
            return True
    except Exception as e:  # never let an owner-check fault grant or crash
        logger.debug("access-tier owner check failed (fail-closed): %s", e)
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
        # else in an allowed chat becomes GROUP_PARTICIPANT (their messages are
        # mention-gated DATA at the dispatcher — never a command/steer turn).
        if chat_type != "dm":
            raw_group = src.get("GROUP_CHAT_ENABLED", "")
            if raw_group is None or not str(raw_group).strip():
                # Unset -> the mode-governed default (explicit env, even falsey,
                # always wins over the default; only the unset case moves).
                group_on = _group_chat_mode_default()
            else:
                group_on = str(raw_group).strip().lower() in _BOOL_TRUE
            if not group_on:
                return AccessTier.DENIED
            chat_id = str(getattr(source, "chat_id", "") or "")
            if not chat_id or not _group_chat_allowed(container, surface, chat_id):
                return AccessTier.DENIED
            # Inside a group, the local-owner bypass is NEVER honoured — group
            # senders are network principals even on a locally-launched surface.
            if _is_owner_or_paired(container, uid, src, allow_local=False):
                return AccessTier.OWNER
            return AccessTier.GROUP_PARTICIPANT

        if _is_owner_or_paired(container, uid, src,
                               allow_local=surface in _LOCAL_OWNER_SURFACES):
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


__all__ = ["AccessTier", "resolve_access_tier"]
