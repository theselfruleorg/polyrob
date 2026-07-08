"""Dynamic (posture/local-derived) flag defaults for the core flag registry.

``core/flags.py`` is core-tier and cannot import the autonomy resolvers that
live here in ``agents.task.constants`` (the local-profile group, the
``AUTONOMY_POSTURE`` sets, the frozen compute posture). This module is the
bridge: :func:`dynamic_flag_default` returns a live ``(value, source_label)``
for flags whose default is NOT static — so ``doctor --flags`` reports what the
running process actually resolves, not just the documented constant.

Explicit env always wins upstream (``core.flags.resolve_flag`` consults this
hook only when the flag is unset), so this changes no behavior anywhere.
"""
from typing import Optional

from agents.task.constants import (
    _POSTURE_FULL_FLAGS,
    _POSTURE_OWNER_VISIBLE_FLAGS,
    _SAFE_LOCAL_FLAGS,
    _posture_autonomy_default,
    _safe_autonomy_default,
    autonomy_posture,
    compute_posture,
    local_mode_enabled,
)


def dynamic_flag_default(name: str) -> Optional[tuple]:
    """Live default for posture/local-governed flags; None = use static default."""
    in_local_group = name in _SAFE_LOCAL_FLAGS
    in_posture_group = name in _POSTURE_FULL_FLAGS or name in _POSTURE_OWNER_VISIBLE_FLAGS
    if in_local_group or in_posture_group:
        local_default = _safe_autonomy_default(name) if in_local_group else False
        posture_default = _posture_autonomy_default(name) if in_posture_group else False
        value = local_default or posture_default
        labels = []
        if in_local_group:
            labels.append(f"local={'ON' if local_mode_enabled() else 'off'}")
        if in_posture_group:
            labels.append(f"posture:{autonomy_posture()}")
        return value, f"default({', '.join(labels)})"
    if name == "AGENT_COMPUTE_POSTURE":
        # Frozen at import — report the frozen value, which is what the process runs.
        return compute_posture(), "default(frozen-at-import)"
    if name == "AUTONOMY_POSTURE":
        return autonomy_posture(), "default"
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
