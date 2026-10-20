"""Goal-board tuning flags (030 file-size extraction).

A mixin of pure access-time env accessors split out of ``AutonomyConfig``
(``core/config_policy/policy.py``) per the god-file ratchet. No behavior
change: `AutonomyConfig` inherits this, every ``AutonomyConfig.goal_*``
call site is untouched. `goals_enabled`/
`goal_self_wake_enabled` STAY in policy.py — they consult the
posture/autonomy-group defaults defined there.
"""
from core.env import bool_env as _bool_env
from core.env import int_env as _int_env

#: Internal tuning (not env-driven). Tests patch these module constants.
#: Trigram similarity at or above which a new goal is a duplicate (0 disables).
GOAL_DEDUP_THRESHOLD = 0.6

#: 083: the ONE default step budget of an interactive request — ``polyrob run``,
#: the HTTP task API, A2A, ``SessionRequest`` and the billed ceiling. Not an env
#: flag. Autonomous runs use ``AutonomyConfig.autonomous_max_steps()``; a chat
#: turn uses ``CHAT_MAX_STEPS``; the REPL uses ``DEFAULT_TURN_BUDGET``.
INTERACTIVE_MAX_STEPS = 50


class GoalFlagsMixin:
    @staticmethod
    def goal_max_retries() -> int:
        return _int_env("GOAL_MAX_RETRIES", 2)

    @staticmethod
    def goal_claim_ttl_sec() -> int:
        return _int_env("GOAL_CLAIM_TTL_SEC", 900)

    @staticmethod
    def goal_yield_for_money_rail() -> bool:
        """056 WS5 (D1): a cron job with ``payload.priority == 'money'`` that comes
        due while a board goal holds the shared workspace pre-empts that goal — the
        run is cancelled at its step boundary, the row returns to ``ready`` with a
        ``resume_note``, the rail runs on the same tick. A human turn is never
        pre-empted. Default OFF (byte-identical); prod arms it."""
        return _bool_env("GOAL_YIELD_FOR_MONEY_RAIL", False)

    @staticmethod
    def goal_yield_grace_sec() -> int:
        """057 WS-C (B8): seconds a pre-empted goal run gets to stop CLEANLY.

        ``0`` (default) = today's behaviour: the run is cancelled immediately at
        whatever point the event loop unwinds it. ``N > 0`` first asks the run to
        stop at its next STEP BOUNDARY (``orchestrator.cancel()``, the cooperative
        flag the run loop already checks), so the step in flight finishes and its
        state is saved; the hard cancel fires only after the grace. Prod arms 90."""
        return _int_env("GOAL_YIELD_GRACE_SEC", 0)

    @staticmethod
    def goal_resume_same_session() -> bool:
        """057 WS-C (B9): re-dispatch a YIELDED goal into its OWN session.

        A yielded goal's history is on disk and the resume machine exists
        (``_recreate_orchestrator`` -> ``load_from_disk``, the self-wake path) —
        goals never used it, so every re-dispatch was a cold ``create_session``
        that re-derived everything. Default OFF (byte-identical); prod arms it."""
        return _bool_env("GOAL_RESUME_SAME_SESSION", False)

    @staticmethod
    def goal_preflight_enabled() -> bool:
        """057 WS-C (B10) / 056 WS5 (2b): refuse to CLAIM a goal whose expected
        runtime crosses the next pre-empting rail's ``next_run_at``.

        Starting a 14-step goal three minutes before the hourly money rail buys a
        guaranteed yield. Default OFF (byte-identical)."""
        return _bool_env("GOAL_PREFLIGHT_ENABLED", False)

    @staticmethod
    def goal_preflight_step_sec() -> int:
        """Fallback seconds-per-step for the pre-flight when this goal has no
        measured history (057 WS-C B10). The measured p95 always wins."""
        return _int_env("GOAL_PREFLIGHT_STEP_SEC", 45)

    @staticmethod
    def goal_yield_ageing() -> int:
        """057 WS-C (B11): priority points a goal gains per RECORDED yield.

        A repeatedly pre-empted goal would otherwise queue behind fresh work
        forever (prod 2026-09-19: one goal yielded 6 times in a night). ``0``
        (default) = off, byte-identical dispatch order."""
        return _int_env("GOAL_YIELD_AGEING", 0)

    @staticmethod
    def autonomous_max_steps() -> int:
        """083 WS-4: the ONE step cap for an autonomous run — goal, cron,
        delegation and worker. The operator owns it; no tool argument, stored
        goal/cron payload or worker profile sets a step budget any more. A
        safety stop, not a task size: cost (``AUTONOMOUS_RUN_BUDGET_USD``) and
        wall clock (``GOAL_MAX_RUN_SECONDS``) are the real bounds. Default 150;
        a value below 1 falls back to the default."""
        value = _int_env("AUTONOMOUS_MAX_STEPS", 150)
        return value if value >= 1 else 150

    @staticmethod
    def goal_dispatch_cron_headroom_sec() -> int:
        """Defer starting a board goal while a cron job is due within this window.

        On a shared project-root workspace a goal run marks the process busy and
        cron ticks skip until it ends, so a goal started minutes before a due money
        rail delays that rail by the goal's whole runtime (prod 2026-09-18: SCOUT
        due 13:30Z ran 13:43Z). ``0`` (default) = off, byte-identical."""
        return _int_env("GOAL_DISPATCH_CRON_HEADROOM_SEC", 0)

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
    def goal_dedup_threshold() -> float:
        return GOAL_DEDUP_THRESHOLD

    @staticmethod
    def goal_daily_quota() -> int:
        """Max goal runs started per trailing 24h; <=0 disables the rail."""
        return _int_env("GOAL_DAILY_QUOTA", 6)


