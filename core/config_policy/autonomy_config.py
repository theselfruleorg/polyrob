"""``AutonomyConfig`` — the flag/cap accessors for the autonomy and continuous-learning loops.

Split out of ``core/config_policy/policy.py`` (S2, 2026-08-29); ``policy.py`` re-exports
every name so existing importers are unaffected. Import order (a DAG): _env -> local_profile
-> autonomy_mode -> autonomy_posture -> compute_posture -> payment_policy -> capability_toggles
-> autonomy_config -> runtime_gates.
"""

import os
from core.config_policy._env import _bool_env, _float_env, _int_env
from core.config_policy.local_profile import _safe_autonomy_default
from core.config_policy.autonomy_posture import _autonomy_group_default, _posture_autonomy_default


from core.config_policy.goal_flags import GoalFlagsMixin


class AutonomyConfig(GoalFlagsMixin):
    """Feature flags + caps for the autonomy/continuous-learning loops.

    Read through this class (not raw os.getenv) so every loop shares one parser and
    the defaults are documented in one place. Evaluated at access time (classmethod /
    property-free) so tests can monkeypatch env between calls.
    """

    # W1 — self-wake rail
    @staticmethod
    def self_wake_enabled() -> bool:
        return _bool_env("SELF_WAKE_ENABLED", _autonomy_group_default("SELF_WAKE_ENABLED"))

    @staticmethod
    def self_wake_max_reentries() -> int:
        return _int_env("SELF_WAKE_MAX_REENTRIES", 3)

    @staticmethod
    def self_wake_idle_backoff_sec() -> float:
        return _float_env("SELF_WAKE_IDLE_BACKOFF_SEC", 30.0)

    # QW-1 (proposal 021) — completion deliverables attach to the owner chat
    @staticmethod
    def deliverables_attach_enabled() -> bool:
        return _bool_env("DELIVERABLES_ATTACH_ENABLED",
                         _autonomy_group_default("DELIVERABLES_ATTACH_ENABLED"))

    # W2 — writable skills + background review
    @staticmethod
    def skills_writable() -> bool:
        return _bool_env("SKILLS_WRITABLE", _autonomy_group_default("SKILLS_WRITABLE"))

    @staticmethod
    def skills_writable_require_review() -> bool:
        return _bool_env("SKILLS_WRITABLE_REQUIRE_REVIEW", True)

    @staticmethod
    def skill_overwrite_protect() -> bool:
        # An agent/background overwrite of an existing ACTIVE skill becomes a .pending
        # proposal (owner promotes); all overwrites archive the prior body. Default ON.
        return _bool_env("SKILL_OVERWRITE_PROTECT", True)

    # polyrob C-write — evolving SELF identity (agent-writable per-(instance,user) doc)
    @staticmethod
    def self_context_writable() -> bool:
        return _bool_env("SELF_CONTEXT_WRITABLE", _autonomy_group_default("SELF_CONTEXT_WRITABLE"))

    @staticmethod
    def self_context_require_review() -> bool:
        return _bool_env("SELF_CONTEXT_REQUIRE_REVIEW", True)

    # Bounded owner-facts doc (USER.md-equivalent) — agent-maintained per-(instance,
    # user) document of durable owner facts/preferences, injected on the SELF/SOUL
    # seam. Same quarantine-then-promote model as SELF; ON under the local profile.
    @staticmethod
    def owner_doc_writable() -> bool:
        return _bool_env("OWNER_DOC_WRITABLE", _autonomy_group_default("OWNER_DOC_WRITABLE"))

    @staticmethod
    def owner_doc_require_review() -> bool:
        return _bool_env("OWNER_DOC_REQUIRE_REVIEW", True)

    # Bounded operating-contract doc (owner-authored operating rules/constraints,
    # owner-UX Phase 2) — injected after owner facts and before the evolving SELF
    # doc on the SELF/SOUL seam. Same quarantine-then-promote model as the owner
    # doc / SELF doc; default ON (unlike the writable-identity flags, this is not
    # gated to the local-safe group — it's a read/inject default, and the writer
    # itself already refuses forged authors + requires review).
    @staticmethod
    def contract_doc_enabled() -> bool:
        return _bool_env("CONTRACT_DOC_ENABLED", True)

    @staticmethod
    def contract_doc_require_review() -> bool:
        return _bool_env("CONTRACT_DOC_REQUIRE_REVIEW", True)

    # §7.1 — self-evolution transparency + owner control loop
    @staticmethod
    def self_evolution_transparency() -> bool:
        return _bool_env("SELF_EVOLUTION_TRANSPARENCY",
                         _autonomy_group_default("SELF_EVOLUTION_TRANSPARENCY"))

    @staticmethod
    def background_review_enabled() -> bool:
        return _bool_env("BACKGROUND_REVIEW_ENABLED", _autonomy_group_default("BACKGROUND_REVIEW_ENABLED"))

    @staticmethod
    def bg_review_interval() -> int:
        return _int_env("BG_REVIEW_INTERVAL", 10)

    @staticmethod
    def bg_review_max_steps() -> int:
        return _int_env("BG_REVIEW_MAX_STEPS", 8)

    # 019 — live run-state observability: master gate for the span/wait feed
    # events (tool_started / llm_started / awaiting_approval / approval_resolved).
    # Default ON, fail-open; OFF restores the pre-019 outcome-only feed.
    @staticmethod
    def run_events_enabled() -> bool:
        return _bool_env("RUN_EVENTS_ENABLED", True)

    # 019 P2 — Telegram live progress bubble (throttled edits of the one
    # ⚙️ Working… status message). Per-owner opt-out via pref progress.telegram.
    @staticmethod
    def telegram_progress_edits() -> bool:
        return _bool_env("TELEGRAM_PROGRESS_EDITS", True)

    # 019 P2 — owner notice when an AUTONOMOUS run (goal/cron) STARTS — today
    # the owner learns only at completion/digest. Same defaulting shape as
    # WAKE_CHANGE_GATE: ON under AUTONOMY_POSTURE=full / autonomous mode.
    @staticmethod
    def autonomy_start_notice() -> bool:
        return _bool_env("AUTONOMY_START_NOTICE",
                         _posture_autonomy_default("AUTONOMY_START_NOTICE"))

    # W3 — cron run-loop + delivery
    @staticmethod
    def cron_run_loop() -> bool:
        return _bool_env("CRON_RUN_LOOP", True)

    @staticmethod
    def cron_delivery_enabled() -> bool:
        return _bool_env("CRON_DELIVERY_ENABLED", False)

    # Owner daily digest: a cron job carrying payload.digest is composed
    # deterministically ($0, no model turn) from the ledger + event log + open
    # asks and pushed via the cron delivery rail. Default OFF.
    @staticmethod
    def owner_digest_enabled() -> bool:
        return _bool_env("OWNER_DIGEST_ENABLED", False)

    # W4 — durable goal board
    @staticmethod
    def goals_enabled() -> bool:
        return _bool_env("GOALS_ENABLED", _autonomy_group_default("GOALS_ENABLED"))

    @staticmethod
    def goal_planner_enabled() -> bool:
        return _bool_env("GOAL_PLANNER_ENABLED", _autonomy_group_default("GOAL_PLANNER_ENABLED"))

    @staticmethod
    def goal_self_wake_enabled() -> bool:
        # Was unconditional; redundant-cost finding (grok livetest 2026-06-27).
        # W1-1: default governed by AUTONOMY_POSTURE (owner-visible/full turn it on).
        return _bool_env("GOAL_SELF_WAKE_ENABLED",
                         _posture_autonomy_default("GOAL_SELF_WAKE_ENABLED"))

    @staticmethod
    def goal_notify_on_done() -> bool:
        # Tell the OWNER when a background goal COMPLETES. Default ON — the owner
        # should hear about successes, not just failures. Decoupled from
        # GOAL_SELF_WAKE_ENABLED (the agent-re-entry feature, posture-gated OFF on
        # the server): the completion push used to live only inside _self_wake, so
        # with self-wake off, completed goals told no one. This is cheap ($0, one
        # push) and controllable — set GOAL_NOTIFY_ON_DONE=false to silence.
        return _bool_env("GOAL_NOTIFY_ON_DONE", True)

    @staticmethod
    def autonomy_halted() -> bool:
        """Owner kill-switch — the ``all`` facet of the 031 pause record
        (``core.autonomy_control``; the legacy halt file/env are read as facets of
        it). Togglable WITHOUT a restart (`polyrob autonomy pause`/`resume`, `/pause`).
        Per-tx/daily caps bound one payment; this stops a looping/compromised agent
        from draining via many sub-cap txs or burning budget in a stall. Fail
        CLOSED: an unreadable record is a pause (see H6 leg 3)."""
        from core.autonomy_control import allows
        return not allows("dispatch").allowed

    @staticmethod
    def entry_paused() -> bool:
        """Owner entry-pause: refuse NEW positions while still allowing exits —
        the ``trading`` scope of the 031 pause record (the legacy entry-pause
        file/env are read as facets of it).

        2026-08-28 intel finding: the 08-26 "no new entries while any open
        position lacks an exit route" directive lived only as prose in the
        ledger + each goal's own reasoning — one stream-manifest goal that
        never referenced it went ahead and traded anyway. Every entry path
        (ad-hoc goal, stream-manifest goal, cron, owner-direct) is gated on this
        one predicate. Exit-shaped intents (sells to quote, revokes) are never
        blocked by it. Fail CLOSED, same as the halt."""
        from core.autonomy_control import allows
        return not allows("trade_entry").allowed

    @staticmethod
    def stream_seeding_paused() -> bool:
        """Owner stream-pause: refuse NEW stream-manifest reseeds while leaving
        everything else alone — the ``streams`` scope of the 031 pause record
        (the legacy stream-pause file/env are read as facets of it).

        2026-09-02 incident: an owner "stop all ghosts" directive cancelled every
        live goal, but the standing streams kept reseeding on schedule because
        they had no code-level awareness of it. Fail CLOSED."""
        from core.autonomy_control import allows
        return not allows("seed_stream").allowed

    # §4.3 (intelligence-stack finalization, 2026-07-09) — evidence-grounded
    # completion review for autonomous runs, DEFAULT ON: the claim (done() text)
    # is judged against the mechanical evidence pack (ledger/artifacts/refs).
    # 'unmet' (claim contradicted) -> record_failure; 'met' -> verified;
    # 'unclear'/error/timeout -> done (UNVERIFIED) — completes but is excluded
    # from the learning loops. Set =false to restore the unjudged legacy path.
    @staticmethod
    def goal_completion_judge() -> bool:
        return _bool_env("GOAL_COMPLETION_JUDGE", True)

    @staticmethod
    def goal_judge_timeout_sec() -> int:
        return _int_env("GOAL_JUDGE_TIMEOUT_SEC", 60)

    # §5.3: ancient blocked goals age out to 'cancelled' (visible, logged)
    # instead of rotting as permanent planner context. 0 disables aging.
    @staticmethod
    def goal_blocked_max_age_days() -> int:
        return _int_env("GOAL_BLOCKED_MAX_AGE_DAYS", 14)

    # T2.1 Task 3 — kind-aware blocked aging: a goal blocked with
    # payload.block_kind='provider_outage' (an LLM/provider death classified by
    # dispatcher._is_llm_provider_exhausted, not a genuinely stuck task) heals on
    # its own — requeue on a MUCH SHORTER window than GOAL_BLOCKED_MAX_AGE_DAYS.
    # Minutes, not days: the provider is expected to recover within the hour.
    @staticmethod
    def goal_blocked_provider_retry_min() -> int:
        return _int_env("GOAL_BLOCKED_PROVIDER_RETRY_MIN", 30)

    # Wake change-gate: a change-gated cron review
    # tick skips the paid model call when the tenant's observable state hasn't
    # moved since the last tick (cron/wake_gate.py). Posture `full` turns it on
    # by default — it pairs with CRON_ENABLED; per-job opt-in via
    # payload.change_gated, delivery jobs never gated.
    @staticmethod
    def wake_change_gate() -> bool:
        return _bool_env("WAKE_CHANGE_GATE",
                         _posture_autonomy_default("WAKE_CHANGE_GATE"))

    # §7.2 — blocker → owner escalation. When a goal trips the circuit breaker
    # (status='blocked') OR the pipeline drains, surface a concrete ask to the owner
    # instead of dying silently. Default OFF (an unsolicited owner push is opt-in).
    @staticmethod
    def goal_blocker_escalation() -> bool:
        # W1-1: default governed by AUTONOMY_POSTURE (owner-visible/full turn it on).
        return _bool_env("GOAL_BLOCKER_ESCALATION",
                         _posture_autonomy_default("GOAL_BLOCKER_ESCALATION"))

    @staticmethod
    def goal_empty_pipeline_escalate_after() -> int:
        """Consecutive planner runs that leave the ready queue EMPTY before the
        stall escalates to the owner (rides GOAL_BLOCKER_ESCALATION)."""
        return _int_env("GOAL_EMPTY_PIPELINE_ESCALATE_AFTER", 2)

    # §7.5 — autonomous continuity bridge. Carry a recent-activity summary INTO a
    # goal/cron tick (opposite scoping to the chat digest) so autonomous runs stop
    # re-deriving "nothing new" every tick. Default OFF (additive context; verify
    # token cost before flipping on).
    @staticmethod
    def autonomous_continuity_bridge() -> bool:
        # W1-1: default governed by AUTONOMY_POSTURE (owner-visible/full turn it on).
        return _bool_env("AUTONOMOUS_CONTINUITY_BRIDGE",
                         _posture_autonomy_default("AUTONOMOUS_CONTINUITY_BRIDGE"))

    # W5 — curator
    @staticmethod
    def curator_enabled() -> bool:
        return _bool_env("CURATOR_ENABLED", _autonomy_group_default("CURATOR_ENABLED"))

    @staticmethod
    def curator_interval_hours() -> int:
        return _int_env("CURATOR_INTERVAL_HOURS", 168)

    @staticmethod
    def curator_stale_days() -> int:
        return _int_env("CURATOR_STALE_DAYS", 30)

    @staticmethod
    def curator_archive_days() -> int:
        return _int_env("CURATOR_ARCHIVE_DAYS", 90)

    # (curator_llm_merge / CURATOR_LLM_MERGE removed 2026-06-29 — the Phase-2 merge step
    #  it gated was a logged no-op with no merge policy. Re-add under its own flag when a
    #  concrete policy exists.)

    # C4 (2026-07-11) — mechanical note consolidation riding the curator tick:
    # archive agent-authored notes never read within the stale window + collapse
    # exact-duplicate notes. LLM-free by design (the aux-LLM clustering/
    # contradiction pass stays deferred until a concrete policy exists — the
    # CURATOR_LLM_MERGE lesson above). Archive-only, audited, never touches
    # owner-authored notes.
    @staticmethod
    def knowledge_curator_enabled() -> bool:
        return _bool_env("KNOWLEDGE_CURATOR_ENABLED",
                         _autonomy_group_default("KNOWLEDGE_CURATOR_ENABLED"))

    @staticmethod
    def knowledge_note_stale_days() -> int:
        return _int_env("KNOWLEDGE_NOTE_STALE_DAYS", 90)

    # W6 — cross-session search tool (read-only, default-on)
    @staticmethod
    def memory_search_tool() -> bool:
        return _bool_env("MEMORY_SEARCH_TOOL", True)

    # W7 — insights tool (read-only authored-skill reuse metric)
    @staticmethod
    def insights_tool() -> bool:
        return _bool_env("INSIGHTS_TOOL", _autonomy_group_default("INSIGHTS_TOOL"))

    # I-6 — agent_status introspection tool (read-only runtime self-report:
    # steps used/remaining, active tools, context usage, wallet+ledger)
    @staticmethod
    def agent_status_tool() -> bool:
        return _bool_env("AGENT_STATUS_TOOL", _safe_autonomy_default("AGENT_STATUS_TOOL"))

    # 2026-08-28 status SSOT — per-turn <live-health> note for the agent (the
    # same health block the owner's /status renders), so "how are you doing?"
    # is answered from live facts, never from stale context. Default ON.
    @staticmethod
    def live_health_context() -> bool:
        return _bool_env("LIVE_HEALTH_CONTEXT", True)

    # I-3 / H3 (dedup decision D1) — verify-before-done: bounded nudge (max 2
    # attempts) when the action ledger shows a code edit newer than the last
    # successful run_tests. See agents/task/runtime/edit_verify.py.
    @staticmethod
    def verify_before_done() -> bool:
        return _bool_env("VERIFY_BEFORE_DONE", _safe_autonomy_default("VERIFY_BEFORE_DONE"))

    # I-2 / H1 (dedup decision D2) — LSP diagnostics-after-edit: after a
    # successful str_replace/apply_patch/create_file, run an external type/lint
    # checker (pyright for .py, tsc for .ts/.tsx/.js/.jsx — see
    # tools/coding/lsp.py::diagnose_file) against the freshly-written file and
    # append an errors-only <diagnostics> block to the tool result. Wired
    # directly into tools/coding/tool.py (NOT a Controller transform hook).
    # Deterministic, no LLM call, fail-open (missing checker/timeout/parse
    # error => no-op). Default OFF and deliberately NOT in _SAFE_LOCAL_FLAGS
    # for v1 — spawns an external subprocess per successful edit; opt-in until
    # proven safe/fast enough to default on under POLYROB_LOCAL.
    @staticmethod
    def coding_lsp_enabled() -> bool:
        return _bool_env("CODING_LSP_ENABLED", False)

    # I-4 / H2 (dedup decision D3) — off-workspace shadow-git per-file
    # snapshot/restore: before a mutating coding action, commit the SINGLE
    # touched file into a shadow git repo living outside the workspace (see
    # tools/coding/snapshot.py), so a bad str_replace/apply_patch/delete is
    # recoverable via the `restore` action. Default OFF and deliberately NOT
    # in _SAFE_LOCAL_FLAGS for v1 — spawns git subprocesses per mutating edit;
    # opt-in until proven safe/fast enough to default on under POLYROB_LOCAL.
    @staticmethod
    def coding_snapshot_enabled() -> bool:
        return _bool_env("CODING_SNAPSHOT_ENABLED", False)

    # KB — knowledge-base feature gate (Task 2 / local_vector prerequisite)
    @staticmethod
    def kb_enabled() -> bool:
        return _bool_env("KB_ENABLED", _safe_autonomy_default("KB_ENABLED"))

    # C1 — context-reference expansion (@file/@folder/@diff/@url)
    # Default ON under POLYROB_LOCAL (single-user CLI), OFF on the server.
    @staticmethod
    def context_references_enabled() -> bool:
        return _bool_env(
            "CONTEXT_REFERENCES_ENABLED",
            _safe_autonomy_default("CONTEXT_REFERENCES_ENABLED"),
        )

    # C9 — auto-load CLAUDE.md/AGENTS.md/.cursorrules as a PROJECT_CONTEXT foundation
    # message. Default ON under POLYROB_LOCAL (single-user CLI), OFF on the server.
    @staticmethod
    def project_context_autoload() -> bool:
        return _bool_env(
            "PROJECT_CONTEXT_AUTOLOAD",
            _safe_autonomy_default("PROJECT_CONTEXT_AUTOLOAD"),
        )

    @staticmethod
    def project_context_max_tokens() -> int:
        return _int_env("PROJECT_CONTEXT_MAX_TOKENS", 20000)

    # Phase 2 — server-side project-context opt-in. When ON (and NOT local mode),
    # the loader runs on the server and the file is injected UNTRUSTED-WRAPPED
    # (framed as DATA, not instructions). Default OFF and deliberately NOT a
    # safe-local flag — POLYROB_LOCAL must not flip it on, so the multi-tenant
    # server stays byte-identical unless an operator explicitly opts in.
    @staticmethod
    def project_context_server_mode() -> bool:
        return _bool_env("PROJECT_CONTEXT_SERVER_MODE", False)

    # T13 — KB auto-prefetch (inject KB recall alongside memory recall at step start)
    # Default ON under POLYROB_LOCAL (single-user CLI), OFF on multi-tenant server.
    @staticmethod
    def kb_auto_prefetch() -> bool:
        return _bool_env("KB_AUTO_PREFETCH", _safe_autonomy_default("KB_AUTO_PREFETCH"))

    # Task 2 — episodic activity ledger (durable per-run provenance rows).
    # Default ON under POLYROB_LOCAL (single-user CLI) OR AUTONOMY_POSTURE
    # owner-visible/full (verified + owner-visible autonomy implies a durable
    # activity ledger, so episodic is part of the posture group).
    @staticmethod
    def episodic_memory_enabled() -> bool:
        return _bool_env("EPISODIC_MEMORY_ENABLED",
                         _autonomy_group_default("EPISODIC_MEMORY_ENABLED")
                         or _posture_autonomy_default("EPISODIC_MEMORY_ENABLED"))

    # Task 3 — inject a recent-episodes digest into the session.
    @staticmethod
    def episodic_digest_inject() -> bool:
        return _bool_env("EPISODIC_DIGEST_INJECT",
                         _autonomy_group_default("EPISODIC_DIGEST_INJECT")
                         or _posture_autonomy_default("EPISODIC_DIGEST_INJECT"))

    # Session-close reflection (consolidate a short session's findings
    # at close; one extra aux call per closed session). Posture-governed so an
    # owner-visible instance actually learns from its autonomous runs. Consumer:
    # modules/memory/task/task_context_manager.py (reads this resolver lazily).
    @staticmethod
    def reflection_on_session_close() -> bool:
        return _bool_env("REFLECTION_ON_SESSION_CLOSE",
                         _posture_autonomy_default("REFLECTION_ON_SESSION_CLOSE"))

    # Restart-durable autonomy state (background delegations + reentry
    # budgets in autonomy_state.db). Default ON; off restores volatile registries.
    @staticmethod
    def autonomy_state_durable() -> bool:
        return _bool_env("AUTONOMY_STATE_DURABLE", True)

    # Task 4 — cross-session continuity bridge (thread_key stitching).
    @staticmethod
    def continuity_bridge_enabled() -> bool:
        return _bool_env("CONTINUITY_BRIDGE_ENABLED", _autonomy_group_default("CONTINUITY_BRIDGE_ENABLED"))

    # Task 4 — LLM-generated continuity summary at reset. Intentionally NOT in
    # _SAFE_LOCAL_FLAGS: OFF everywhere by default (adds latency at reset).
    @staticmethod
    def continuity_llm_summary() -> bool:
        return _bool_env("CONTINUITY_LLM_SUMMARY", False)  # OFF everywhere (latency at reset)

    # Task 2 — episodic row retention window (days); pruned on the curator tick.
    @staticmethod
    def episodic_retention_days() -> int:
        return _int_env("EPISODIC_RETENTION_DAYS", 90)

    # B3 (2026-07-11) — cross-session `memories` retention window (days), enforced
    # on the curator tick via provider.prune_memories. Only rows with a B2
    # provenance stamp are age-prunable (legacy stampless rows are exempt);
    # <=0 disables the sweep entirely. Deliberately generous default — recall
    # rows are cheap and the exact-dup collapse already bounds growth.
    @staticmethod
    def memory_retention_days() -> int:
        return _int_env("MEMORY_RETENTION_DAYS", 365)

    # T16 — interrupt-and-redirect: Ctrl-C mid-turn prompts for a redirect instruction
    # that becomes the next turn instead of silently aborting. Default OFF; NOT in
    # _SAFE_LOCAL_FLAGS (must be opt-in — changes SIGINT UX for all local users).
    @staticmethod
    def interrupt_redirect_enabled() -> bool:
        return _bool_env("INTERRUPT_REDIRECT", False)
