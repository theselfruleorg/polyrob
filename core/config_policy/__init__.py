"""core.config_policy — cross-cutting config/policy (WS-1). See policy.py."""
from core.config_policy.policy import *  # noqa: F401,F403
from core.config_policy.policy import (  # noqa: F401  (underscored names: explicit)
    _AUTONOMY_LOCAL_FLAGS,
    _FALSEY,
    _MODE_CAPABILITY_FLAGS,
    _POSTURE_FULL_FLAGS,
    _POSTURE_OWNER_VISIBLE_FLAGS,
    _SAFE_LOCAL_FLAGS,
    _autonomy_enabled_default,
    _autonomy_group_default,
    _bool_env,
    _int_env,
    _mode_capability_default,
    _posture_autonomy_default,
    _refreeze_compute_posture,
    _refreeze_compute_posture_for_tests,
    _refreeze_payment_approval_flags,
    _refreeze_payment_approval_flags_for_tests,
    _safe_autonomy_default,
    reset_autonomy_mode_warnings,
)
from core.config_policy._env import posture_flag, safe_local_flag  # noqa: E402,F401
from core.config_policy.compute_posture import compute_posture_allows_safe  # noqa: E402,F401
from core.config_policy.builder_mode import (  # noqa: E402 — 032 bundle
    _builder_capability_default,
    agent_builder_mode,
    builder_mode_display,
    effective_builder_mode,
    publish_enabled,
    reset_builder_mode_warnings,
    ship_clamp_reason,
)
