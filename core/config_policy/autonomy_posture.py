"""``AUTONOMY_POSTURE`` (which autonomy loops run) and the ``AUTONOMY_ENABLED`` master for the
local autonomy bucket.

Split out of ``core/config_policy/policy.py`` (S2, 2026-08-29); ``policy.py`` re-exports
every name so existing importers are unaffected. Import order (a DAG): _env -> local_profile
-> autonomy_mode -> autonomy_posture -> compute_posture -> payment_policy -> capability_toggles
-> autonomy_config -> runtime_gates.
"""

import os
from core.config_policy._env import _bool_env
from core.config_policy.local_profile import _AUTONOMY_LOCAL_FLAGS, local_mode_enabled
from core.config_policy.autonomy_mode import full_autonomy_enabled


def _autonomy_enabled_default() -> bool:
    """Default for the ``AUTONOMY_ENABLED`` master switch.

    True when the operator has deliberately opted into an autonomous posture or
    mode — ``AUTONOMY_MODE=autonomous`` (effective) or ``AUTONOMY_POSTURE`` in
    {owner-visible, full} — so a deliberate autonomy setting is never left with a
    silently-inert local autonomy layer. Otherwise False: new local installs are
    autonomy-OFF until ``AUTONOMY_ENABLED`` is set. Access-time (sees bootstrap env).
    """
    return full_autonomy_enabled() or autonomy_posture() in ("owner-visible", "full")


def autonomy_enabled() -> bool:
    """``AUTONOMY_ENABLED`` — the single owner-legible switch for the local
    autonomy loop group (self-wake, goal board + planner, curator,
    background-review, episodic continuity, self-writing). Default OFF for a new
    local install; ON when an autonomous posture/mode is set (see
    :func:`_autonomy_enabled_default`). An explicit ``AUTONOMY_ENABLED`` env always
    wins.

    This is the master; the individual autonomy flags additionally require
    ``POLYROB_LOCAL`` (they are the local profile's autonomous subset — see
    :func:`_autonomy_group_default`). Server behavior is unchanged (local off =>
    the whole group off regardless of this switch).
    """
    return _bool_env("AUTONOMY_ENABLED", _autonomy_enabled_default())


def _autonomy_group_default(flag_name: str) -> bool:
    """Default for an AUTONOMY-bucket local flag: ON only under local mode AND
    autonomy enabled. The ``local_mode_enabled()`` conjunction is the server
    byte-identity contract (local off => False, always)."""
    if flag_name not in _AUTONOMY_LOCAL_FLAGS:
        return False
    return local_mode_enabled() and autonomy_enabled()


# --- W1-1: AUTONOMY_POSTURE — one coherent switch for the shipped-but-dark loops ----
#
# Five autonomy flags shipped wired but default-OFF in BOTH modes, each behind its own
# env var, so making an instance actually verify + report its autonomous work meant
# flipping five independent flags with no single lever (the "activation, not machinery"
# gap). AUTONOMY_POSTURE is a second axis (orthogonal to _SAFE_LOCAL_FLAGS) that moves
# the DEFAULTS of that group together. An explicit per-flag env ALWAYS wins (only the
# default moves), and the unset/`silent` posture is byte-identical to today.
#
#   silent        (default) — today's behavior: autonomy runs but is unverified + silent.
#   owner-visible — the agent's autonomous work becomes VERIFIED + owner-visible:
#                   completion judge, blocker->owner escalation (+ ask), self-wake
#                   delivery, continuity bridge. Safe for single-user local; on a
#                   multi-tenant server it is an opt-in (unsolicited pushes / aux cost).
#   full          — owner-visible PLUS time-based initiative (cron ticker).
_POSTURE_OWNER_VISIBLE_FLAGS = frozenset({
    "GOAL_COMPLETION_JUDGE",
    "GOAL_BLOCKER_ESCALATION",
    "GOAL_SELF_WAKE_ENABLED",
    "AUTONOMOUS_CONTINUITY_BRIDGE",
    # Continuity/learning trio: ON under the local profile, and an owner-visible
    # posture also turns it on server-side (memory + verification, no
    # unsolicited-cost initiative). Explicit env wins.
    "EPISODIC_MEMORY_ENABLED",
    "EPISODIC_DIGEST_INJECT",
    "REFLECTION_ON_SESSION_CLOSE",
})


# NOTE: AUTONOMY_START_NOTICE was a member here until 2026-09-15 and is now OFF
# in every posture (the owner opts in explicitly). Measured on prod over
# 2026-09-08..15: the `self_evolution` lifecycle source made 223 delivery
# attempts and 29 reached the owner — 87% produced only to be suppressed, and
# `▶ goal started` was the single largest occupant of the owner's `/missed`
# store. "A run began" is already on the goal board and the console; a chat ping
# per start is volume, not information. Completion notices are untouched.
_POSTURE_FULL_FLAGS = _POSTURE_OWNER_VISIBLE_FLAGS | {
    "CRON_ENABLED", "WAKE_CHANGE_GATE",
}


_AUTONOMY_POSTURES = ("silent", "owner-visible", "full")


def autonomy_posture() -> str:
    """Resolved AUTONOMY_POSTURE (silent|owner-visible|full).

    Default `silent` in BOTH modes (byte-identical to pre-W1-1). An unknown value
    degrades to `silent` so a typo never silently activates autonomy. Access-time so
    tests/bootstrap env changes are seen.
    """
    raw = (os.getenv("AUTONOMY_POSTURE") or "").strip().lower()
    if raw in _AUTONOMY_POSTURES:
        return raw
    return "full" if full_autonomy_enabled() else "silent"


def _posture_autonomy_default(flag_name: str) -> bool:
    """Default for a posture-governed autonomy flag, given the resolved posture."""
    posture = autonomy_posture()
    if posture == "full":
        return flag_name in _POSTURE_FULL_FLAGS
    if posture == "owner-visible":
        return flag_name in _POSTURE_OWNER_VISIBLE_FLAGS
    return False  # silent: today's defaults
