"""Named tool rigs for autonomous (cron / goal) sessions — 057 WS-A.

**The problem this closes.** Every autonomous run shipped the FULL grant: a cron
job's toolset is ``payload.tools`` else ``default_cron_tools()``, a goal's is
``payload.tools`` else ``default_goal_tools()``, and no prod cron job declared
``payload.tools`` at all (0 of 30 enabled, 2026-09-20). So a read-only watcher
and an on-chain buyback shipped the same ~180 tool schemas, and the only
narrowing site in the tree (``goal_create``'s keyword inference) *widens*.

**The shape.** ONE table of named rigs with a FIXED id order, and one pure
resolver with an explicit precedence:

1. ``payload.tools`` — the existing contract, honoured VERBATIM. An owner (or
   an operator seat: ``/trade``, ``polyrob goals create --tools``) writes it and
   it is a grant; a rig must never be able to overturn it.
2. ``payload.rig`` — a name from :data:`RIGS`.
3. ``AUTONOMOUS_RIG_DEFAULT`` — the deploy-wide default rig name.
4. the caller's own default (``default_cron_tools()`` / ``default_goal_tools()``).

``full`` is a real rig NAME that resolves to *the caller's default* rather than
to a frozen list: the default is posture- and mode-aware (compute tools at
posture>=1, the ship rails under ``AGENT_BUILDER_MODE``), and freezing a copy
here would be a second home for a fact that already has one.

⚠️ **A rig is a REQUEST, not a grant — but only because of the ceiling.**
Until 2026-09-23 this docstring claimed ``goal_create`` strips money tools from
an agent-authored rig. It did not: it stripped ``tools=`` and stored ``rig=``
unchecked, so ``rig="money_rail"`` resolved to ``defi_trade`` at dispatch
(``social`` -> ``x_browser``, ``ops`` -> ``cronjob``/``goal``) — security
analysis H05. Two layers now hold that line:

* creation — ``goal_create`` / ``cronjob_schedule`` REFUSE a rig whose ids
  leave ``allowed_self_goal_tools()``, and stamp ``authored_by="agent"``;
* dispatch — :func:`resolve_rig_tools` intersects a rig on an AGENT-AUTHORED
  payload (:func:`is_agent_authored`) with the ``agent_ceiling`` the caller
  passes, so a row written before the refusal existed is narrowed too.

An owner seat (``polyrob cron rig``, ``/trade``, ``polyrob goals create``)
stamps no agent marker, so an owner-set rig is honoured as written. So is a
rig ``goal_create`` / ``cronjob_schedule`` write on a GENUINE owner turn (the
owner asked in chat): those stamp ``authored_by="owner"``. Under the ``armed``
money regime (``core/config_policy/money_regime.py``) the ceiling itself holds
``defi_trade``, so a self-authored ``money_rail`` passes too. Every
execution-time gate (money authority in ``load_tools_from_container``, the
delegation blocklist, taint/posture/approval) still runs on top.

⚠️ ``message`` is a controller ACTION id, not a container tool id (the two
vocabularies, 055 §2.4). ``load_tools_from_container`` handles that explicitly
(``tool_load_report.action_id_gap``): a registered action is "nothing to load
and nothing missing". It is in every rig on purpose — an autonomous run that
cannot speak to its owner is the failure mode, not the saving.

Default ``AUTONOMOUS_RIG_DEFAULT=full`` => byte-identical to pre-057.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from core.config_policy.profiles import names as _profile_names, profile as _profile

logger = logging.getLogger(__name__)

#: The rig name that means "whatever the caller would have used anyway".
FULL_RIG = "full"

#: rig name -> the ids it requests, in a FIXED order. The order is part of the
#: contract: the emitted tool-schema bytes ride the prompt-cache prefix, so two
#: runs of the same rig must produce the same bytes (WS-A, "stable tool bytes").
#: The ids live in ``core/config_policy/profiles.py`` as ``rig:<name>`` (067 P1 —
#: one ordered table; the ``rig:`` prefix keeps them apart from the colliding
#: ``toolset:research``/``toolset:social``/``toolset:full``). What each rig is for:
#:
#: * ``money_rail`` — the on-chain rail. ``defi_trade`` is listed so a granted
#:   run is NARROW, not so an ungranted one becomes able to trade: on an
#:   agent-authored row the ceiling (allowed_self_goal_tools) drops it unless
#:   the money regime is armed (core/config_policy/money_regime.py).
#: * ``social`` — build-in-public: the API rail and the browser rail, plus what
#:   it takes to read a source and draft from it.
#: * ``research`` — read the world, write notes. No comms rail beyond
#:   ``message`` (the owner).
#: * ``ops`` — the harness talking to itself: schedule, queue, report.
RIGS: Dict[str, Tuple[str, ...]] = {
    name: _profile(f"rig:{name}") for name in _profile_names("rig")
}


def rig_names() -> List[str]:
    """Every valid rig name, including ``full``. Sorted for stable help text."""
    return sorted(set(RIGS) | {FULL_RIG})


def is_rig(name: Optional[str]) -> bool:
    return bool(name) and str(name).strip().lower() in set(rig_names())


def rig_tools(name: Optional[str]) -> Optional[List[str]]:
    """The id list for *name*, or None for ``full`` / an unknown name.

    None means "no opinion — use the caller's default". An UNKNOWN name is
    logged and treated as no-opinion (fail-open): a typo in an env var or a
    stored payload must not tool-starve a money rail.
    """
    key = (name or "").strip().lower()
    if not key:
        return None
    if key == FULL_RIG:
        return None
    ids = RIGS.get(key)
    if ids is None:
        logger.warning(
            "unknown tool rig %r — falling back to the caller's default toolset "
            "(valid rigs: %s)", name, ", ".join(rig_names()))
        return None
    # 067 P3: RIGS is an import-time view; a pack disabled at phase 2 drops out here.
    from core.config_policy.profiles import provided_only
    return provided_only(ids)


def default_rig_name() -> str:
    """``AUTONOMOUS_RIG_DEFAULT`` — the deploy-wide default rig. Default ``full``."""
    raw = (os.getenv("AUTONOMOUS_RIG_DEFAULT") or "").strip().lower()
    return raw or FULL_RIG


#: Payload provenance for an AGENT-authored goal / cron row. An owner seat never
#: writes it. ``created_by_session_id`` is the older goal-only stamp that
#: ``goal_create`` has written on every call since 031 — honoured too, so a row
#: written before ``authored_by`` existed is still recognised as the agent's.
AUTHORED_BY_KEY = "authored_by"
AGENT_AUTHOR = "agent"
#: A goal / cron row the agent's tool wrote on a GENUINE OWNER TURN (the owner
#: asked for it in chat — ``tools.goal_tools.owner_seat_turn``). It is treated
#: like an owner-seat row: its rig is honoured as written. Only code writes a
#: payload (the tools build it field by field), so the model cannot forge it.
OWNER_AUTHOR = "owner"


def is_agent_authored(payload: Optional[Mapping]) -> bool:
    """True when the agent (not an owner seat) wrote this payload.

    An explicit ``authored_by`` wins: ``owner`` is NOT agent-authored even
    though ``goal_create`` also stamps ``created_by_session_id`` on it. The
    session stamp alone marks a row written before ``authored_by`` existed."""
    if not isinstance(payload, Mapping):
        return False
    author = str(payload.get(AUTHORED_BY_KEY) or "").strip().lower()
    if author:
        return author != OWNER_AUTHOR
    return bool(payload.get("created_by_session_id"))


def rig_ungrantable_ids(name: Optional[str],
                        ceiling: Sequence[str]) -> List[str]:
    """The ids rig *name* requests that *ceiling* does not allow — ``[]`` for
    ``full``, an unknown name, or a rig that fits. Creation-time check."""
    ids = rig_tools(name) if is_rig(name) else None
    if not ids:
        return []
    allowed = set(ceiling)
    return [t for t in ids if t not in allowed]


def resolve_rig_tools(
    payload: Optional[Mapping],
    default_tools: Optional[Sequence[str]] = None,
    *,
    agent_ceiling: Optional[Sequence[str]] = None,
) -> Optional[List[str]]:
    """Resolve the toolset for one autonomous run. Pure; no I/O beyond env.

    Precedence: ``payload.tools`` (verbatim) > ``payload.rig`` >
    ``AUTONOMOUS_RIG_DEFAULT`` > *default_tools*.

    ``agent_ceiling`` (H05): when given AND the payload is agent-authored, the
    ids the rig resolves to are intersected with it (order kept) — a
    ``payload.rig`` AND the deploy-wide ``AUTONOMOUS_RIG_DEFAULT``. The default
    is the operator's, but it is a DEFAULT: ``AUTONOMOUS_RIG_DEFAULT=money_rail``
    must not hand ``defi_trade`` to work the agent wrote itself unless the money
    regime put it in the ceiling (the flag map, 2026-09-23). An owner-authored
    row is never intersected.

    Returns ``None`` only when *default_tools* is None and nothing else applied
    — the signal a caller (the goal dispatcher) uses to fall through to its own
    richer default resolution (child inheritance, dispatch-time inference).
    """
    data = payload if isinstance(payload, Mapping) else {}
    own = data.get("tools")
    if own:
        return list(own)
    # An EXPLICIT rig on the row — including `full` — outranks the deploy-wide
    # default: a job that says "I want everything" must not be narrowed by an
    # operator setting AUTONOMOUS_RIG_DEFAULT later. A row with no rig key (or a
    # typo, which rig_tools has already logged) falls through to the env.
    named = str(data.get("rig") or "").strip().lower()
    if named and not is_rig(named):
        rig_tools(named)  # logs the unknown-rig warning, returns None
        named = ""
    ids = rig_tools(named) if named else rig_tools(default_rig_name())
    if ids is not None and agent_ceiling is not None and is_agent_authored(data):
        allowed = set(agent_ceiling)
        dropped = [t for t in ids if t not in allowed]
        if dropped:
            logger.warning(
                "rig %r on an agent-authored payload: %s NOT granted (outside "
                "the self-goal ceiling)", named or default_rig_name(),
                ", ".join(dropped))
            ids = [t for t in ids if t in allowed]
    if ids is not None:
        return ids
    return list(default_tools) if default_tools is not None else None


#: 060 WS-5: the most skills one rail may pin (seeds bypass max_skills, so the
#: bound lives here — a pinned list is doctrine, not a library).
MAX_PINNED_SKILLS = 8


def pinned_skills(payload: Optional[Mapping]) -> List[str]:
    """The skill ids a rail's payload PINS (``payload.skills``), cleaned.

    A list of non-empty strings, deduplicated in order, at most
    :data:`MAX_PINNED_SKILLS`. Anything else (absent, a string, junk) is ``[]`` —
    the run then keyword-matches exactly as before. Pinning loads doctrine TEXT
    only; it grants no tool (the toolset is still ``resolve_rig_tools``'s).
    """
    raw = (payload or {}).get("skills") if isinstance(payload, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return []
    out = [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    return list(dict.fromkeys(out))[:MAX_PINNED_SKILLS]
