"""Env parsers with POLYROB's falsey-set semantics (thin wrappers over ``core.env``, kept as the
re-exported ``_bool_env``/``_int_env``/``_float_env`` names).

Split out of ``core/config_policy/policy.py`` (S2, 2026-08-29); ``policy.py`` re-exports
every name so existing importers are unaffected. Import order (a DAG): _env -> local_profile
-> autonomy_mode -> autonomy_posture -> compute_posture -> payment_policy -> capability_toggles
-> autonomy_config -> runtime_gates.
"""

from core.env import bool_env as _core_bool_env, int_env as _core_int_env, float_env as _core_float_env


# --- Autonomy & continuous-learning loops (Reference-parity, 2026-06-16) ---------
#
# Shared flag helpers for the four loops POLYROB lacked: self-wake re-entry,
# writable-skills + background-review, cron run-loop+delivery, durable goal board,
# and the curator. SINGLE SOURCE OF TRUTH for the falsey-set semantics, mirroring
# reflection_llm_enabled_default() above. All loops default-OFF + fail-open except
# MEMORY_SEARCH_TOOL (read-only, tenant-scoped) and CRON_RUN_LOOP (fixes a live bug
# where cron built a session but never ran the agent loop).

_FALSEY = ("none", "off", "false", "0", "no", "")


def _bool_env(name: str, default: bool) -> bool:
    """Read a boolean env var with POLYROB's falsey-set semantics.

    Delegates to the repo-wide SSOT (``core.env.bool_env``) so this module shares
    one parser with everything else instead of reimplementing it (the reflection-gate
    bug was a parser/source mismatch). Kept as a thin wrapper so in-module callers
    (and this module's public ``_bool_env`` symbol) are unaffected.
    """
    return _core_bool_env(name, default)


def _int_env(name: str, default: int) -> int:
    """Delegates to the ONE int parser (core.env.int_env); name kept for re-export."""
    return _core_int_env(name, default)


def _float_env(name: str, default: float) -> float:
    """Delegates to the ONE float parser (core.env.float_env); mirrors _int_env."""
    return _core_float_env(name, default)
