"""Goal-board / planner tuning flags (030 file-size extraction).

A mixin of pure access-time env accessors split out of ``AutonomyConfig``
(``core/config_policy/policy.py``) per the god-file ratchet. No behavior
change: `AutonomyConfig` inherits this, every ``AutonomyConfig.goal_*``
call site is untouched. `goals_enabled`/`goal_planner_enabled`/
`goal_self_wake_enabled` STAY in policy.py — they consult the
posture/autonomy-group defaults defined there.
"""
from core.env import bool_env as _bool_env
from core.env import float_env as _float_env
from core.env import int_env as _int_env


class GoalFlagsMixin:
    @staticmethod
    def goal_max_retries() -> int:
        return _int_env("GOAL_MAX_RETRIES", 2)

    @staticmethod
    def goal_claim_ttl_sec() -> int:
        return _int_env("GOAL_CLAIM_TTL_SEC", 900)

    @staticmethod
    def goal_max_run_seconds() -> int:
        """H11: hard wall-clock cap on a single goal run (mirrors cron's per-job cap).
        A goal is otherwise bounded only by max_steps, so one hung step (tool/LLM/browser)
        blocks forever and permanently occupies a GOAL_MAX_CONCURRENT slot."""
        return _int_env("GOAL_MAX_RUN_SECONDS", 1800)

    @staticmethod
    def goal_dispatch_interval_sec() -> int:
        return _int_env("GOAL_DISPATCH_INTERVAL_SEC", 60)

    @staticmethod
    def goal_max_concurrent() -> int:
        return _int_env("GOAL_MAX_CONCURRENT", 2)

    @staticmethod
    def goal_fair_dispatch() -> bool:
        """Round-robin the ready queue across objectives instead of a global order."""
        return _bool_env("GOAL_FAIR_DISPATCH", True)

    @staticmethod
    def goal_per_objective_cap() -> int:
        """Max concurrent goal runs for ONE objective. <=0 disables the extra cap."""
        return _int_env("GOAL_PER_OBJECTIVE_CAP", 0)

    @staticmethod
    def goal_dedup_threshold() -> float:
        return _float_env("GOAL_DEDUP_THRESHOLD", 0.6)

    @staticmethod
    def goal_planner_min_ready() -> int:
        return _int_env("GOAL_PLANNER_MIN_READY", 2)

    @staticmethod
    def goal_planner_cooldown_sec() -> int:
        return _int_env("GOAL_PLANNER_COOLDOWN_SEC", 3600)

    @staticmethod
    def goal_planner_history_n() -> int:
        return _int_env("GOAL_PLANNER_HISTORY_N", 10)

    @staticmethod
    def goal_planner_scaling() -> bool:
        """Derive the planner's ceilings + service order from the objective count.

        ONE revert for the whole planner half of the stream-scaling work: `=false`
        restores the pre-scaling shape — a fixed ready ceiling of
        ``PLANNER_READY_FLOOR`` (the old "Never exceed 5 ready goals total"
        literal), the fixed ``GOAL_PLANNER_MIN_READY`` thinness gate, and no
        "SERVE THESE OBJECTIVES FIRST" block in the prompt. ``GOAL_FAIR_DISPATCH``
        reverts dispatch; this reverts planning.
        """
        return _bool_env("GOAL_PLANNER_SCALING", True)

    @staticmethod
    def goal_planner_goals_per_run() -> int:
        """Upper bound on goals ONE planner run may create."""
        return _int_env("GOAL_PLANNER_GOALS_PER_RUN", 3)

    @staticmethod
    def goal_planner_ready_ceiling() -> int:
        """Ready-goal ceiling the planner is told to respect. <=0 derives it."""
        return _int_env("GOAL_PLANNER_READY_CEILING", 0)

    @staticmethod
    def goal_planner_max_social() -> int:
        """Max goals in one planner run that may carry the `twitter` tool."""
        return _int_env("GOAL_PLANNER_MAX_SOCIAL", 1)

    @staticmethod
    def goal_daily_quota() -> int:
        """Max goal runs started per trailing 24h; <=0 disables the rail."""
        return _int_env("GOAL_DAILY_QUOTA", 6)


