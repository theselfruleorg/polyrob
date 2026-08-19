"""Dynamic (posture/local-derived) flag defaults for the core flag registry.

:func:`dynamic_flag_default` returns a live ``(value, source_label)`` for flags
whose default is NOT static — the local-profile group, the ``AUTONOMY_POSTURE``
sets, the frozen compute posture — so ``doctor --flags`` reports what the
running process actually resolves, not just the documented constant.

History: this lived in ``agents/task/flag_defaults.py`` as a bridge module
because ``core/flags.py`` could not import the autonomy resolvers while they
lived in ``agents.task.constants``. WS-1 (2026-07-16) moved the resolvers to
:mod:`core.config_policy`, so the bridge now lives WITH them; the old path
re-exports. The hook stays caller-injected (``core.flags.resolve_flag``
consults it only when passed and the flag is unset), so plain resolution keeps
its static defaults and this changes no behavior anywhere.
"""
from typing import Optional

from core.config_policy.policy import (
    _AUTONOMY_LOCAL_FLAGS,
    _MODE_CAPABILITY_FLAGS,
    _POSTURE_FULL_FLAGS,
    _POSTURE_OWNER_VISIBLE_FLAGS,
    _SAFE_LOCAL_FLAGS,
    _autonomy_group_default,
    _mode_capability_default,
    _posture_autonomy_default,
    _safe_autonomy_default,
    autonomy_enabled,
    autonomy_posture,
    compute_posture,
    full_autonomy_enabled,
    local_mode_enabled,
)


def dynamic_flag_default(name: str) -> Optional[tuple]:
    """Live default for posture/local-governed flags; None = use static default."""
    in_local_group = name in _SAFE_LOCAL_FLAGS
    in_autonomy_group = name in _AUTONOMY_LOCAL_FLAGS
    in_posture_group = name in _POSTURE_FULL_FLAGS or name in _POSTURE_OWNER_VISIBLE_FLAGS
    if in_local_group or in_autonomy_group or in_posture_group:
        local_default = _safe_autonomy_default(name) if in_local_group else False
        autonomy_default = _autonomy_group_default(name) if in_autonomy_group else False
        posture_default = _posture_autonomy_default(name) if in_posture_group else False
        value = local_default or autonomy_default or posture_default
        labels = []
        if in_local_group:
            labels.append(f"local={'ON' if local_mode_enabled() else 'off'}")
        if in_autonomy_group:
            labels.append(f"autonomy={'ON' if autonomy_enabled() else 'off'}")
        if in_posture_group:
            labels.append(f"posture:{autonomy_posture()}")
        return value, f"default({', '.join(labels)})"
    if name in _MODE_CAPABILITY_FLAGS:
        # 026 P0.1: the AUTONOMY_MODE capability group. The label always names the
        # EFFECTIVE mode (a clamped `autonomous` request reads supervised) so the
        # report can never claim a capability the runtime denies.
        effective_autonomous = full_autonomy_enabled()
        return (
            _mode_capability_default(name),
            f"default(mode:{'autonomous' if effective_autonomous else 'supervised'})",
        )
    if name == "AGENT_COMPUTE_POSTURE":
        # Frozen at import — report the frozen value, which is what the process runs.
        return compute_posture(), "default(frozen-at-import)"
    if name == "PAYMENT_APPROVAL_MODE":
        # Frozen at import AND mode-dependent default (auto under effective
        # autonomous) — report what the process actually froze (026 P0.2).
        from core.config_policy.policy import payment_approval_mode
        return payment_approval_mode(), "default(frozen-at-import)"
    if name == "AUTONOMY_POSTURE":
        return autonomy_posture(), "default"
    if name == "AUTONOMY_ENABLED":
        # Master switch for the local autonomy-loop group (T1) — report the live
        # resolved value, labeled as DERIVED (mode/posture raise it), so
        # "why is autonomy on/off?" has an answer in `doctor --flags`.
        return autonomy_enabled(), "default(mode/posture-derived)"
    if name in ("POLYROB_LOCAL", "ROB_LOCAL"):
        return local_mode_enabled(), "default(process)"
    if name in _LOCAL_DERIVED_EXTRAS:
        # local-mode-derived defaults that live OUTSIDE _SAFE_LOCAL_FLAGS
        # (resolved via local_mode_enabled()/bool_env("POLYROB_LOCAL") inline)
        on = local_mode_enabled()
        return on, f"default(local={'ON' if on else 'off'})"
    return None


# Flags whose default is local_mode-derived but NOT via _SAFE_LOCAL_FLAGS:
# TICKER_IDLE_BACKOFF_ENABLED (agents/task/constants.py, local_mode_enabled()
# inline) and MEMORY_STORE_ANSWER_ONLY (modules/memory/sqlite_memory_provider.py,
# bool_env("POLYROB_LOCAL", ...) inline).
_LOCAL_DERIVED_EXTRAS = frozenset({
    "TICKER_IDLE_BACKOFF_ENABLED",
    "MEMORY_STORE_ANSWER_ONLY",
})
