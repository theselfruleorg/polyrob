"""Cross-cutting config/policy surface for POLYROB — FACADE.

The autonomy/mode/posture/payment policy cluster and AutonomyConfig were relocated here from
agents/task/constants.py (WS-1, 2026-07-16) and split into nine submodules (S2, 2026-08-29):

    _env               falsey-set env parsers (_bool_env/_int_env/_float_env)
    local_profile      POLYROB_LOCAL buckets + local_mode_enabled
    autonomy_mode      AUTONOMY_MODE master switch + single-owner clamp
    autonomy_posture   AUTONOMY_POSTURE + AUTONOMY_ENABLED
    compute_posture    AGENT_COMPUTE_POSTURE ladder (frozen) + compute_posture_allows
    payment_policy     PAYMENT_APPROVAL_MODE / timeout / grant TTL (frozen) + refreeze
    capability_toggles per-feature toggles and runtime knobs
    autonomy_config    AutonomyConfig
    runtime_gates      embedder_needed

This module re-exports every public and externally-referenced private name, so
``from core.config_policy.policy import X`` and ``core.config_policy.policy.X`` keep working
(tests patch/read attributes here; the module-level frozen state lives in the owning submodule
and the re-exported accessor/refreeze functions are the SAME objects, so state stays coherent).
The package imports ONLY stdlib + core.env (+ lazy core.instance / core.security.forged_turns),
so `import core.config_policy` never reaches agents.task. New code imports from
core.config_policy (the package).
"""

import logging  # noqa: F401  (namespace parity with the pre-split module)
import os  # noqa: F401

from core.config_policy._env import (  # noqa: F401
    _FALSEY,
    _bool_env,
    _float_env,
    _int_env,
)
from core.config_policy.local_profile import (  # noqa: F401
    _AUTONOMY_LOCAL_FLAGS,
    _SAFE_LOCAL_FLAGS,
    _safe_autonomy_default,
    local_mode_enabled,
)
from core.config_policy.autonomy_mode import (  # noqa: F401
    _AUTONOMY_MODES,
    _FULL_AUTONOMY_WARNED,
    _MODE_CAPABILITY_FLAGS,
    _mode_capability_default,
    autonomy_mode,
    autonomy_mode_display,
    full_autonomy_clamp_reason,
    full_autonomy_enabled,
    reset_autonomy_mode_warnings,
)
from core.config_policy.autonomy_posture import (  # noqa: F401
    _AUTONOMY_POSTURES,
    _POSTURE_FULL_FLAGS,
    _POSTURE_OWNER_VISIBLE_FLAGS,
    _autonomy_enabled_default,
    _autonomy_group_default,
    _posture_autonomy_default,
    autonomy_enabled,
    autonomy_posture,
)
from core.config_policy.compute_posture import (  # noqa: F401
    _COMPUTE_POSTURE_FROZEN,
    _refreeze_compute_posture,
    _refreeze_compute_posture_for_tests,
    _resolve_compute_posture,
    compute_posture,
    compute_posture_allows,
)
from core.config_policy.payment_policy import (  # noqa: F401
    PAYMENT_APPROVAL_TOOLS,
    PAYMENT_RECEIVE_APPROVAL_TOOLS,
    _FROZEN_APPROVAL_GRANT_TTL_HOURS,
    _FROZEN_PAYMENT_APPROVAL_MODE,
    _FROZEN_PAYMENT_APPROVAL_TIMEOUT_SEC,
    _refreeze_payment_approval_flags,
    _refreeze_payment_approval_flags_for_tests,
    _snapshot_approval_grant_ttl_hours,
    _snapshot_payment_approval_mode,
    _snapshot_payment_approval_timeout_sec,
    approval_grant_ttl_hours,
    payment_approval_mode,
    payment_approval_timeout_sec,
)
from core.config_policy.capability_toggles import (  # noqa: F401
    compaction_prompt_guard,
    dead_target_registry_enabled,
    defi_data_enabled,
    defi_trade_enabled,
    eip8004_payment_feedback_enabled,
    email_provider,
    hmem_tail_placement,
    invoice_card_enabled,
    memory_backend_default,
    memory_prefetch_cadence,
    message_autonomous_allowlisted,
    message_tool_enabled,
    owner_message_cooldown_seconds,
    prefs_tool_enabled,
    reflection_llm_enabled_default,
    resolved_memory_backend,
    run_budget_usd,
    task_personality_block_enabled,
    ticker_idle_backoff_enabled,
    ticker_idle_backoff_max_multiplier,
    tool_progressive_disclosure,
)
from core.config_policy.autonomy_config import (  # noqa: F401
    AutonomyConfig,
    GoalFlagsMixin,
)
from core.config_policy.runtime_gates import (  # noqa: F401
    embedder_needed,
)
