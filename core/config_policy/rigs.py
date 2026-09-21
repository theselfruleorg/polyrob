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

⚠️ **A rig is a REQUEST, not a grant.** Every existing gate still runs on top:
``goal_create`` strips money tools from an agent-authored payload,
``load_tools_from_container`` refuses a money id without owner authority, the
delegation blocklist holds for leaves, and taint/posture/approval are
execution-time gates. Listing ``defi_trade`` in ``money_rail`` does not grant it
— it only stops the OWNER-granted money rail from also shipping 170 schemas it
will never call.

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

logger = logging.getLogger(__name__)

#: The rig name that means "whatever the caller would have used anyway".
FULL_RIG = "full"

#: rig name -> the ids it requests, in a FIXED order. The order is part of the
#: contract: the emitted tool-schema bytes ride the prompt-cache prefix, so two
#: runs of the same rig must produce the same bytes (WS-A, "stable tool bytes").
RIGS: Dict[str, Tuple[str, ...]] = {
    # The owner-granted on-chain rail. `defi_trade` is listed so a granted run
    # is NARROW, not so an ungranted one becomes able to trade — goal_create
    # strips money ids from anything the agent authors itself.
    "money_rail": ("defi_data", "defi_trade", "filesystem", "task", "message"),
    # Build-in-public: the API rail and the browser rail, plus what it takes to
    # read a source and draft from it.
    "social": ("twitter", "x_browser", "filesystem", "task", "message", "web_fetch"),
    # Read the world, write notes. No comms rail beyond `message` (the owner).
    "research": ("web_fetch", "anysite", "knowledge", "filesystem", "task", "message"),
    # The harness talking to itself: schedule, queue, report.
    "ops": ("filesystem", "task", "goal", "cronjob", "message"),
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
    return list(ids)


def default_rig_name() -> str:
    """``AUTONOMOUS_RIG_DEFAULT`` — the deploy-wide default rig. Default ``full``."""
    raw = (os.getenv("AUTONOMOUS_RIG_DEFAULT") or "").strip().lower()
    return raw or FULL_RIG


def resolve_rig_tools(
    payload: Optional[Mapping],
    default_tools: Optional[Sequence[str]] = None,
) -> Optional[List[str]]:
    """Resolve the toolset for one autonomous run. Pure; no I/O beyond env.

    Precedence: ``payload.tools`` (verbatim) > ``payload.rig`` >
    ``AUTONOMOUS_RIG_DEFAULT`` > *default_tools*.

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
    if ids is not None:
        return ids
    return list(default_tools) if default_tools is not None else None
