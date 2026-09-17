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

from core.env import _FALSEY  # noqa: E402 — the ONE falsey set, re-exported by name


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


def posture_flag(name: str, min_posture: int) -> bool:
    """A flag that defaults ON at ``AGENT_COMPUTE_POSTURE >= min_posture``.

    An explicit env value always wins (e.g. force-off at a raised posture);
    a posture probe error means the default is OFF. Three tool packages
    (shell, self_env, code_exec) each re-derived this by hand.
    """
    try:
        from core.config_policy.compute_posture import compute_posture
        default = compute_posture() >= min_posture
    except Exception:
        default = False
    return _bool_env(name, default)


def safe_local_flag(name: str) -> bool:
    """A flag in the ``POLYROB_LOCAL`` safe group: an explicit env value wins
    (parsed default-OFF), otherwise the interactive-local default
    (``_safe_autonomy_default``). coding/git each re-derived this by hand."""
    import os
    if os.getenv(name) is not None:
        return _bool_env(name, False)
    from core.config_policy.policy import _safe_autonomy_default
    return _safe_autonomy_default(name)
