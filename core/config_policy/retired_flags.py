"""Retired env flags — the names an older env file may still set.

A retired flag has NO reader: its row is gone from ``docs/CONFIGURATION.md``
and the code keeps the behaviour the flag defaulted to. Most retired names are
harmless if still set (a revert switch that nothing reads). A few are legacy
ALIASES whose canonical name decides an identity, a posture or money — an env
file that still sets only the old name would silently change who the owner is,
which data home the instance opens, how the console isolates tenants, or which
deposit addresses derive. Those REFUSE (``refuse_retired_alias``) at the read
site that used to honour them, naming the rename. Everything else is reported
by ``polyrob doctor`` so the owner can delete the line.

Retired in 1.4 (the 1.3 "Retire after 1.4" markers), and after 1.4 the
duplicate model pickers (one way to pick the model: ``DEFAULT_PROVIDER`` +
``DEFAULT_MODEL``, ``HEAVY_MODEL``, ``AUX_*``, a job's ``payload.model``).
A per-provider ``POLYROB_<PROVIDER>_MODEL`` line is matched by pattern
(:data:`RETIRED_PATTERNS`), not spelled.

Moved into ``POLYROB_OVERRIDES`` (owner decision D7, 2026-10-10): the 50
posture members (``core.config_policy.members.MEMBER_FLAGS``) are no longer env
settings of their own. A direct line is ignored — never refused — and
:func:`retired_flag_lines` names the exact replacement line
(``POLYROB_OVERRIDES=GIT_TOOLS_ENABLED=false``). The names are not spelled
here: the member sets are the one list.

This module is the ONE place a retired name is spelled in shipped code. It is
not a flag table: ``tests/unit/core/test_flags_reverse.py`` asserts no name
here is also a catalog row.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

#: name -> the setting that replaces it ("" = nothing: the new behaviour is
#: permanent and the line can be deleted).
RETIRED_FLAGS: dict[str, str] = {
    # legacy aliases — set the canonical name instead
    "ROB_LOCAL": "POLYROB_LOCAL",
    "WEBGATE_MULTITENANT": "POLYROB_POSTURE=multitenant",
    "OWNER_SURFACES": "OWNER_SURFACE",
    "BOT_INSTANCE_ID": "POLYROB_INSTANCE_ID",
    "BOT_OWNER_EMAIL": "POLYROB_OWNER_EMAIL",
    "BOT_OWNER_USER_ID": "POLYROB_OWNER_USER_ID",
    "MASTER_SEED": "PAYMENT_MASTER_SEED",
    "LAZY_DEPS_ENABLED": "LAZY_DEPS_MODE",
    "GROUP_REQUIRE_MENTION": "GROUP_DEFAULT_MODE",
    "AUX_PROVIDER": "AUX_PROVIDER_<SLOT>",
    "COMPACTION_MODEL": "AUX_MODEL_COMPACTION",
    "COMPACTION_AUX_MODEL": "AUX_MODEL_COMPACTION",
    "COMPACTION_PROVIDER": "AUX_PROVIDER_COMPACTION",
    "POLYROB_ENV_KEY_BACKFILL": "polyrob config migrate",
    # one way to pick the model — the duplicate pickers
    "CHAT_PROVIDER": "DEFAULT_PROVIDER",
    "CHAT_MODEL": "DEFAULT_MODEL",
    "COMPACTION_AUTO_AUX": "AUX_AUTO",
    # revert switches — the default-ON behaviour is now permanent
    "AUTONOMY_SHUTDOWN_DRAIN": "",
    "AUTONOMY_STATE_DURABLE": "",
    "CLI_SUPPRESS_DONE_RECAP": "",
    "CLI_TODO_SESSION_SCOPED": "",
    "COMPACTION_WARM_PREFIX": "",
    "CONVERSATION_RESUME_ENABLED": "",
    "CORRESPONDENT_RESOLVE_LATEST": "",
    "CTX_COUNT_TOOL_SCHEMAS": "",
    "DOC_KIND_ENFORCED": "",
    "FOUNDATION_REPLAY": "",
    "GOAL_FAIR_DISPATCH": "",
    "GOAL_PLANNER_SCALING": "",
    "HMEM_TAIL_PLACEMENT": "",
    "INTERACTIVE_GATE_MARKER": "",
    "KB_AUTO_INGEST_ARTIFACTS": "",
    "OPENROUTER_PROMPT_CACHE": "",
    "OWNER_RULES_SUPERSEDE": "",
    "OWNER_THREAD_ENABLED": "",
    "POLYROB_PERSISTENT_INPUT": "",
    "RUN_EVENTS_ENABLED": "",
    "SECURITY_EVENT_LOG_ENABLED": "",
    "SELF_CONTEXT_COMBINED": "",
    "SKILL_PROGRESSIVE_DISCLOSURE": "",
    "STATE_MESSAGE_EPHEMERAL": "",
    "SURFACE_OWNER_TURN": "",
    "TOOL_CATALOG_TAIL_UPDATES": "",
    "TOOL_SCHEMA_STABLE_ORDER": "",
    "TOOL_USAGE_TELEMETRY": "",
    "TX_NOTIFY_ENABLED": "",
    # 080 WS-6 — rails, objectives, streams and the objective planner are
    # deleted; a recurring need is a cron job (`polyrob cron`, `/cron`)
    "GOAL_PLANNER_ENABLED": "",
    "GOAL_PLANNER_MIN_READY": "",
    "GOAL_PLANNER_COOLDOWN_SEC": "",
    "GOAL_PLANNER_GOALS_PER_RUN": "",
    "GOAL_PLANNER_READY_CEILING": "",
    "GOAL_PLANNER_MAX_SOCIAL": "",
    "GOAL_EMPTY_PIPELINE_ESCALATE_AFTER": "",
    "GOAL_PER_OBJECTIVE_CAP": "",
    "OBJECTIVE_GOAL_BUDGET": "",
    "STREAM_SEEDING_PAUSE": "",
    "POLYROB_STREAMS_MANIFEST": "",
    # 083 — the step cap is the operator's: one cap for every autonomous run
    "GOAL_DEFAULT_MAX_STEPS": "AUTONOMOUS_MAX_STEPS",
}

#: The aliases whose silent loss changes identity, posture or money. Their old
#: read sites call :func:`refuse_retired_alias` instead of reading the value.
REFUSING_ALIASES: dict[str, str] = {
    "BOT_INSTANCE_ID": "POLYROB_INSTANCE_ID",
    "BOT_OWNER_USER_ID": "POLYROB_OWNER_USER_ID",
    "MASTER_SEED": "PAYMENT_MASTER_SEED",
    "WEBGATE_MULTITENANT": "POLYROB_POSTURE",
    # a silently ignored model pin would change the model every run uses
    "CHAT_PROVIDER": "DEFAULT_PROVIDER",
    "CHAT_MODEL": "DEFAULT_MODEL",
}

#: Retired name PATTERNS (regex -> replacement): a family no code reads.
RETIRED_PATTERNS: dict[str, str] = {
    r"POLYROB_[A-Z0-9_]+_MODEL": "DEFAULT_MODEL (pinned provider) or payload.model",
}


class RetiredFlagError(RuntimeError):
    """An env file sets a retired alias whose canonical name is unset."""


#: Boolean aliases: only a TRUE value ever changed behaviour, so a false one
#: (``WEBGATE_MULTITENANT=false``) is merely a stale line, never a refusal.
_BOOLEAN_ALIASES = frozenset({"WEBGATE_MULTITENANT", "ROB_LOCAL"})


def _set(env: Mapping[str, str], name: str) -> bool:
    raw = str(env.get(name) or "").strip()
    if name in _BOOLEAN_ALIASES and raw:
        from core.env import parse_bool
        return parse_bool(raw, False) is True
    return raw != ""


def refuse_retired_alias(name: str, env: Optional[Mapping[str, str]] = None) -> None:
    """Raise :class:`RetiredFlagError` when *name* (a REFUSING alias) is set and
    its canonical name is not. A no-op otherwise — the canonical name, when set,
    wins as it always did, so a file that carries both keeps working."""
    canonical = REFUSING_ALIASES[name]
    env = os.environ if env is None else env
    if _set(env, name) and not _set(env, canonical):
        raise RetiredFlagError(
            f"{name} was retired and is no longer read. Rename it to "
            f"{canonical} in your env file (same value) and restart. POLYROB "
            f"refuses to start on the old name alone instead of silently "
            f"falling back to a default."
        )


def retired_flags_set(env: Optional[Mapping[str, str]] = None) -> list[tuple[str, str]]:
    """``[(name, replacement)]`` for every retired name *env* still sets."""
    import re
    env = os.environ if env is None else env
    out = [(n, r) for n, r in RETIRED_FLAGS.items() if _set(env, n)]
    for rx, replacement in RETIRED_PATTERNS.items():
        out += [(n, replacement) for n in sorted(env)
                if re.fullmatch(rx, n) and _set(env, n)]
    return out


def moved_into_overrides(env: Optional[Mapping[str, str]] = None) -> list[tuple[str, str]]:
    """``[(member, replacement line)]`` for every posture member *env* still sets
    on its own line (D7: moved into ``POLYROB_OVERRIDES``; ignored, not refused)."""
    from core.config_policy.members import members_set_directly, set_line
    return [(n, set_line(n, v)) for n, v in members_set_directly(env)]


def retired_flag_lines(env: Optional[Mapping[str, str]] = None) -> list[str]:
    """Doctor lines: one per retired name still set, one per posture member set
    directly, one per ignored ``POLYROB_OVERRIDES`` entry (empty when none)."""
    out = []
    for name, replacement in retired_flags_set(env):
        if name in REFUSING_ALIASES and not _set(
                os.environ if env is None else env, REFUSING_ALIASES[name]):
            out.append(f"FAIL retired flag {name} is set: rename it to "
                       f"{REFUSING_ALIASES[name]} (POLYROB refuses the old name)")
        elif replacement:
            out.append(f"WARN retired flag {name} is set and ignored: use "
                       f"{replacement} instead, then delete the line")
        else:
            out.append(f"WARN retired flag {name} is set and ignored: its "
                       f"default is now permanent — delete the line")
    from core.config_policy.members import member_env_lines
    out.extend(member_env_lines(env))
    return out


__all__ = [
    "RETIRED_FLAGS", "RETIRED_PATTERNS", "REFUSING_ALIASES", "RetiredFlagError",
    "moved_into_overrides", "refuse_retired_alias", "retired_flags_set",
    "retired_flag_lines",
]
