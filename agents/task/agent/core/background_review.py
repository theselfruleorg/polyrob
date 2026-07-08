"""Post-turn background-review fork (W2-C, Reference-parity self-improvement loop).

After a run of productive turns, the agent forks a cheap, fire-and-forget sub-agent
that reviews what just happened and — when it finds a durable, reusable procedure —
authors a skill (via the W2 ``skill_manage`` tool, which quarantines it for review).
This is the "learning" loop: experience → distilled skill → available next session.

Design:
- **Non-blocking** — ``asyncio.create_task`` so the run loop returns immediately; a
  review never delays the user-facing turn.
- **Cheap** — runs on the aux/judge model (``_provision_aux_llm("judge")``) and
  inherits the cached system prefix → prefix-cache hit (Reference's ~26% saving).
- **Least-privilege** — spawned through ``SubAgentManager.run_subtask`` as a depth-1
  LEAF (UP-05 child controller: no code-exec/cron, cannot re-delegate).
- **Bounded** — at most ``BG_REVIEW_MAX_STEPS`` steps; exempt for sub-agents (a
  reviewer never forks its own reviewer).
- **Fail-open** — any error is swallowed; the loop must never break on a review.

Gated ``BACKGROUND_REVIEW_ENABLED`` (default OFF). Authored skills are quarantined
(``created_by="background_review"`` → always ``.pending/``), so even a compromised
review cannot self-activate a skill.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_REVIEW_PROMPT = (
    "You are a background reviewer for an AI agent. The agent just finished a run of "
    "work in this session. Review the recent conversation/work and decide whether a "
    "DURABLE, REUSABLE procedure emerged that would help in future sessions.\n\n"
    "If yes: call skill_manage(action='create', skill_id=<short-kebab-id>, "
    "content=<a concise SKILL.md: a '# Title' heading then when-to-use + steps>). "
    "Keep it general (no secrets, no session-specific data). Author at most ONE skill.\n"
    "If nothing is worth saving: call done() with a one-line reason. Do not do any "
    "other work, browse, or message the user."
)

# polyrob Phase E: the "learns over time" engine. The reviewer already runs as a
# LEAF sub-agent, so a self_context_manage write resolves to background provenance →
# ALWAYS .pending, NEVER auto-active and NEVER promotable by this forged turn. So we
# can safely invite it to ALSO propose a consolidated SELF-context refinement; the
# owner promotes it later. Only offered when SELF_CONTEXT_WRITABLE is on.
_SELF_CONTEXT_REVIEW_ADDENDUM = (
    "\n\nSEPARATELY: if you learned something durable about how to work with THIS user "
    "(a stable preference, convention, or fact worth remembering), you may also call "
    "self_context_manage(action='update', content=<a consolidated self.md, ≤2200 "
    "chars — MERGE with what's already there via action='read' first; do not sprawl>). "
    "This proposes a refinement to your evolving SELF context; it is QUARANTINED for "
    "owner review and applies next session. Keep it about working-style/preferences, "
    "never your core identity or boundaries (those are operator-owned)."
)


def build_review_prompt() -> str:
    """The reviewer task. Adds the SELF-context-refinement invitation only when the
    SELF_CONTEXT_WRITABLE tool is actually available (else the legacy skills-only
    prompt, byte-identical)."""
    try:
        from agents.task.constants import AutonomyConfig
        if AutonomyConfig.self_context_writable():
            return _REVIEW_PROMPT + _SELF_CONTEXT_REVIEW_ADDENDUM
    except Exception:
        pass
    return _REVIEW_PROMPT


class BackgroundReviewMixin:
    """Fire a post-turn self-improvement reviewer. Composed into Agent."""

    def _bg_review_should_fire(self, turn_was_productive: bool) -> bool:
        """Pure decision: increment the productive-turn counter and report whether the
        interval has elapsed. Resets the counter when it fires."""
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.background_review_enabled():
            return False
        if not AutonomyConfig.skills_writable():
            # SK-F8: the reviewer's whole output rail is skill_manage(create) —
            # firing it while skills are read-only just burns an aux-model run
            # that is guaranteed to fail at the tool layer.
            return False
        if getattr(self, "_is_sub_agent", False):
            return False  # a reviewer never forks a reviewer
        if not turn_was_productive:
            return False
        interval = max(1, AutonomyConfig.bg_review_interval())
        n = getattr(self, "_bg_review_productive_turns", 0) + 1
        if n >= interval:
            self._bg_review_productive_turns = 0
            return True
        self._bg_review_productive_turns = n
        return False

    def _maybe_spawn_background_review(self, *, turn_was_productive: bool) -> None:
        """Fire-and-forget the reviewer if the cadence says so. Never blocks/raises."""
        try:
            if not self._bg_review_should_fire(turn_was_productive):
                return
            task = asyncio.create_task(self._run_background_review())
            # M5: keep a strong reference so CPython can't GC the task mid-run (a task no
            # one references may be collected before it completes), and so it's cancelled
            # at session teardown. Mirrors _stall_check_task; self-removes when done.
            _tasks = getattr(self.orchestrator, "_execution_tasks", None)
            if isinstance(_tasks, list):
                _tasks.append(task)
                task.add_done_callback(lambda t, _l=_tasks: (_l.remove(t) if t in _l else None))
            logger.info("🪞 spawned background-review fork (session %s)",
                        getattr(self.orchestrator, "session_id", "?"))
        except Exception as e:
            logger.debug("background-review spawn skipped: %s", e)

    async def _run_background_review(self) -> None:
        """Run the reviewer sub-agent on the aux model. Fail-open."""
        try:
            from agents.task.constants import AutonomyConfig
            manager = self.orchestrator.get_sub_agent_manager() if self.orchestrator else None
            if manager is None:
                return
            # P2-8: reuse the agent's cached judge client instead of building a fresh
            # isolated client (with its own httpx pool) on every fire — the old code
            # leaked one pool per background review. Cached on self._judge_llm (the same
            # slot _validate_output uses) and closed once at session cleanup (M2).
            aux = getattr(self, "_judge_llm", None)
            if aux is None:
                try:
                    # P2-9: async provisioning — don't block the loop building the client.
                    aux = await self._provision_aux_llm_async("judge")
                    if aux is not None:
                        self._judge_llm = aux
                except Exception:
                    aux = None
            # P2-10: make the reviewer's model cost VISIBLE. The cheap-aux map has no
            # openrouter/nvidia key, so on the default OpenRouter deploy `aux` is None and
            # this PROACTIVE loop (fires every BG_REVIEW_INTERVAL turns without any user
            # ask) runs up to bg_review_max_steps on the FLAGSHIP main model. Log at INFO
            # which model it will use so the cost isn't silent (add an openrouter/nvidia
            # cheap-map entry or set AUX_MODEL_JUDGE to route it cheap).
            _review_llm = aux or self.llm
            if aux is None:
                logger.info(
                    "background review running on the MAIN model %r (no cheap aux "
                    "resolved) — set AUX_MODEL_JUDGE or add a provider cheap-map entry "
                    "to route it to a cheaper model",
                    getattr(self.llm, "model_name", getattr(self.llm, "model_type", "?")),
                )
            await manager.run_subtask(
                task=build_review_prompt(),
                parent_agent_id=self.agent_id,
                profile_id="executor",
                max_steps=AutonomyConfig.bg_review_max_steps(),
                parent_llm=_review_llm,
                skip_complexity_check=True,
            )
        except Exception as e:
            logger.debug("background-review run failed (ignored): %s", e)
