"""Memory prefetch mixin (roadmap P7 / B-T2 — Reference-parity).

Routes the agent's current query through the active memory provider
(``modules.memory.registry``) at the start of a step and injects any recalled
context as a one-shot ``RECALL``-origin message. Inert by default: the registry's
default provider is ``NullMemoryProvider`` (returns ""), so nothing is injected and
production behaviour is unchanged until an external provider is registered.

Fail-open throughout — a memory backend hiccup never breaks the agent loop.
"""
from __future__ import annotations

import logging
from typing import Optional

from modules.llm.messages import BaseMessage
from agents.task.constants import memory_prefetch_cadence

logger = logging.getLogger(__name__)


async def build_prefetch_message(query: str, *, session_id: str,
                                 user_id: Optional[str] = None) -> Optional[BaseMessage]:
    """Prefetch memory for ``query`` and wrap it as a MEMORY control message.

    Returns None when there is nothing to inject (Null provider, empty recall, or any
    error). Otherwise a ``HumanMessage`` enveloped with ``MessageOrigin.RECALL``.
    ``user_id`` scopes recall to the tenant (P0-0).

    When ``AutonomyConfig.kb_auto_prefetch()`` is True, ALSO queries the active KB
    collection and appends the result under a distinct section header. The KB branch
    is fully independent and fail-open: a KB error never breaks the memory branch.
    When ``kb_auto_prefetch()`` is False the function is byte-identical to today.
    """
    if not query or not query.strip():
        return None
    try:
        from modules.memory.registry import memory_prefetch

        recalled = await memory_prefetch(query, session_id=session_id, user_id=user_id)
    except Exception as e:  # registry/provider failure must never break the step
        logger.debug("memory prefetch skipped: %s", e)
        return None

    if recalled:
        recalled = recalled.strip() or ""
    else:
        recalled = ""

    # T4-02: keep the RAW recall for the observability preview — the event must
    # show what was recalled, not the untrusted-wrap envelope around it.
    raw_recalled = recalled

    # Cross-session recall replays prior user/assistant turns verbatim, so an
    # instruction injected into one session could be recalled into another. Frame
    # it as DATA (same delimiter the untrusted tool-result path uses) so indirect
    # prompt-injection via recalled memory is read, not obeyed. Gated by the shared
    # UNTRUSTED_TOOL_RESULT_WRAP flag (default ON).
    if recalled:
        try:
            from agents.task.constants import UNTRUSTED_TOOL_RESULT_WRAP
            if UNTRUSTED_TOOL_RESULT_WRAP:
                from agents.task.agent.core.untrusted_wrap import wrap_untrusted
                recalled = wrap_untrusted("cross_session_memory", recalled)
        except Exception as e:  # framing must never break prefetch
            logger.debug("memory recall wrap skipped: %s", e)

    # --- T13: KB auto-prefetch (additive, fail-open, fully independent) -------
    kb_recalled = ""
    raw_kb = ""
    try:
        from agents.task.constants import AutonomyConfig
        if AutonomyConfig.kb_auto_prefetch():
            try:
                from modules.memory.registry import kb_search
                # Auto-prefetch collection is configurable — a KB ingested into a
                # non-"default" collection was previously never auto-recalled because
                # this was hardcoded. Operators point it at their collection via env.
                import os
                _kb_collection = os.getenv("KB_PREFETCH_COLLECTION", "default").strip() or "default"
                kb_result = await kb_search(query, user_id=user_id, collection=_kb_collection)
                if kb_result and kb_result.strip():
                    kb_recalled = kb_result.strip()
                    raw_kb = kb_recalled
                    try:
                        from agents.task.constants import UNTRUSTED_TOOL_RESULT_WRAP
                        if UNTRUSTED_TOOL_RESULT_WRAP:
                            from agents.task.agent.core.untrusted_wrap import wrap_untrusted
                            kb_recalled = wrap_untrusted("knowledge_base", kb_recalled)
                    except Exception as e:  # wrap failure must never break KB branch
                        logger.debug("kb recall wrap skipped: %s", e)
            except Exception as e:  # KB error must never break memory branch
                logger.debug("kb auto-prefetch skipped: %s", e)
    except Exception as e:  # flag-read failure is non-fatal
        logger.debug("kb_auto_prefetch flag check skipped: %s", e)
    # --------------------------------------------------------------------------

    # Combine: sections when both present; single section when only one; None if neither.
    if recalled and kb_recalled:
        combined = f"## Recalled from memory\n\n{recalled}\n\n## Recalled from knowledge base\n\n{kb_recalled}"
    elif recalled:
        combined = recalled
    elif kb_recalled:
        combined = kb_recalled
    else:
        return None

    # T4-02: recall was invisible on every surface (ephemeral LLM message only) —
    # record a first-class memory_recall event so the owner can reconstruct what
    # was recalled and acted on. Raw (pre-wrap) content; fail-open.
    _event_attrs = None
    try:
        from agents.task.telemetry.memory_events import emit_memory_event, scrubbed_preview
        _scope = ("cross_session+kb" if (raw_recalled and raw_kb)
                  else ("kb" if raw_kb else "cross_session"))
        _raw = "\n".join(p for p in (raw_recalled, raw_kb) if p)
        _event_attrs = emit_memory_event(
            "memory_recall", user_id=user_id or "", session_id=session_id,
            source="prefetch", scope=_scope, content=_raw,
            query=scrubbed_preview(query))
    except Exception as e:
        logger.debug("memory recall event skipped: %s", e)

    from modules.llm.messages import make_control_message, MessageOrigin

    msg = make_control_message(combined, MessageOrigin.RECALL)
    if _event_attrs:
        try:
            # Ride the attrs on the message so the caller (the prefetch mixin,
            # which holds the orchestrator) can mirror the event to the live feed.
            msg._memory_event = _event_attrs
        except Exception:
            pass
    return msg


class MemoryPrefetchMixin:
    """Compose into Agent to inject recalled memory at step start (B-T2)."""

    def _build_recall_query(self) -> str:
        """Build the memory-recall query from the live turn, not just the static task.

        Combines the task with the current brain-state ``Next`` (the agent's stated
        next intent) so recall tracks what the agent is actually doing now (P0-1).
        Fail-open: falls back to the task string alone on any error.
        """
        task = getattr(self, "task", "") or ""
        try:
            # Real attribute set in next_action_internal.py:646; field name is
            # next_goal (AgentBrain, views.py:39).  The old names (state.current_brain,
            # _last_brain, .next) were all wrong → brain was always None → permanent
            # no-op.  Also accept dict form for robustness.
            # INVARIANT: the `memory` field must NEVER enter the query.
            brain = getattr(self, "_last_brain_state", None)
            nxt = ""
            if isinstance(brain, dict):
                nxt = str(brain.get("next_goal") or brain.get("next") or "")
            else:
                nxt = str(getattr(brain, "next_goal", "") or "")
            query = f"{task} {nxt}".strip()
            return query or task
        except Exception:
            return task

    async def _maybe_prefetch_memory(self) -> None:
        """Inject a recalled-memory message on a cadence. Fail-open.

        Always fires on the first step. With cadence N>0 it ALSO fires every N steps
        (resolved via ``memory_prefetch_cadence()`` — 0/first-step-only on the server,
        3 by default under POLYROB_LOCAL, explicit env wins). Uses the task as the
        recall query and queues the result as an ephemeral (one-shot) message so it
        rides along on the next LLM call without bloating persistent history.
        """
        try:
            # P2-4: a delegated leaf sub-agent gets no automatic cross-session recall —
            # every other injector (episodic digest / autonomy continuity / continuity
            # bridge) already excludes sub-agents, and sub-agents are deliberately H-MEM-
            # isolated (NullTaskContextManager). Provider recall bypassed that on the read
            # side, polluting a focused subtask's context with tenant-wide memory.
            if getattr(self, "_is_sub_agent", False):
                return
            n_steps = getattr(self.state, "n_steps", 0)
            # SA-06: an autonomous (goal/cron) session defaults to a recurring
            # cadence — recalling once at step 1 (where the brain enrichment is
            # empty) left long missions memory-blind. Fail-open to non-autonomous.
            try:
                from agents.task.goals.autonomy_marker import is_autonomous
                _autonomous = is_autonomous(self.session_id)
            except Exception:
                _autonomous = False
            cadence = memory_prefetch_cadence(autonomous=_autonomous)
            should = (n_steps == 1) or (cadence > 0 and n_steps > 0 and n_steps % cadence == 0)
            if not should:
                return
            msg = await build_prefetch_message(
                self._build_recall_query(),
                session_id=self.session_id,
                user_id=getattr(self, "user_id", None),
            )
            if msg is not None:
                self.message_manager.push_ephemeral_message(msg)
                self.logger.debug("Injected prefetched memory block")
                # T4-02: mirror the recall event to the live session feed so the
                # CLI (/verbose) and webview watchers see it as it happens.
                attrs = getattr(msg, "_memory_event", None)
                if attrs:
                    try:
                        await self.orchestrator.add_to_feed(
                            getattr(self, "agent_id", "agent"), "memory_recall", dict(attrs))
                    except Exception as feed_err:
                        self.logger.debug(f"memory recall feed mirror skipped: {feed_err}")
        except Exception as e:
            self.logger.debug(f"memory prefetch injection skipped: {e}")

    async def _maybe_inject_episodic_digest(self) -> None:
        """Inject a recent-activity digest of the agent's OWN recent runs, ONCE, on
        the first step of a chat/owner session (Task 5). Fail-open; inert unless
        ``AutonomyConfig.episodic_digest_inject()`` is on and an external episodic
        provider has rows in-window.

        Scoping (the key correctness property — never leak into an autonomous run):
        this is invoked from the first step of the RUN LOOP, not at Agent
        construction time. ``agents.task.runtime.run_as_session.run_task_as_session``
        calls ``mark_autonomous(session_id)`` AFTER ``create_session()`` returns but
        BEFORE ``run_session()`` starts stepping — so at construction time a
        goal/cron/planner session would not yet be marked autonomous and would be
        misread as a chat session. By the time step 1 runs, the marker is reliably
        set, so ``is_autonomous(self.session_id)`` here is trustworthy.
        """
        try:
            if getattr(self.state, "n_steps", 0) != 1:
                return
            if getattr(self, "_session_bootstrap_done", False):
                return
            if getattr(self, "_is_sub_agent", False):
                return
            from agents.task.goals.autonomy_marker import is_autonomous
            if is_autonomous(self.session_id):
                return
            from agents.task.agent.core.episodic_digest import build_activity_digest
            msg = await build_activity_digest(
                user_id=getattr(self, "user_id", None),
                kind="chat",
                is_sub_agent=self._is_sub_agent,
            )
            if msg is not None:
                self.message_manager.push_ephemeral_message(msg)
                self.logger.debug("Injected episodic activity digest")
        except Exception as e:
            self.logger.debug(f"episodic digest injection skipped: {e}")

    async def _maybe_inject_autonomous_continuity(self) -> None:
        """§7.5: carry recent activity INTO an autonomous goal/cron tick so it stops
        re-deriving 'nothing new' each time. Mirror-image scoping of the chat digest —
        fires ONLY for an autonomous session (never chat), first-step-only, never a
        sub-agent. Gated AUTONOMOUS_CONTINUITY_BRIDGE (default OFF). Fail-open.

        Once per SESSION, like its siblings: a self-wake re-entry resets n_steps
        back to 1, so without the bootstrap-done guard the continuity block would
        be re-injected on every wake (D6)."""
        try:
            if getattr(self.state, "n_steps", 0) != 1:
                return
            if getattr(self, "_session_bootstrap_done", False):
                return
            if getattr(self, "_is_sub_agent", False):
                return
            from agents.task.goals.autonomy_marker import is_autonomous
            if not is_autonomous(self.session_id):
                return  # chat sessions use _maybe_inject_episodic_digest instead
            from agents.task.agent.core.episodic_digest import build_mission_continuity
            msg = await build_mission_continuity(user_id=getattr(self, "user_id", None))
            if msg is not None:
                self.message_manager.push_ephemeral_message(msg)
                self.logger.debug("Injected autonomous continuity bridge")
        except Exception as e:
            self.logger.debug(f"autonomous continuity injection skipped: {e}")

    async def _maybe_inject_continuity_bridge(self) -> None:
        """Seed a SESSION_BRIDGE 'last time we were discussing X' message, ONCE, on the
        first step of a chat/owner session (Task 6). Fail-open; inert unless
        ``AutonomyConfig.continuity_bridge_enabled()`` is on and a prior chat episode
        with a summary exists for this session's ``thread_key``.

        Scoped identically to ``_maybe_inject_episodic_digest`` (first-step-only,
        never sub-agent, never autonomous) — see that method's docstring for why the
        autonomy-marker check is only reliable from the first step of the run loop,
        not at Agent construction time.

        The new session's OWN chat episode isn't written until ITS OWN cleanup, so
        recalling "most-recent chat episode for this thread_key" here can only ever
        find the PRIOR session's episode — no self-exclusion needed.

        ``thread_key`` is resolved via a reverse lookup on the durable
        ``session_chat_registry`` (mirrors ``TaskAgent._rebind_recreated_chat`` in
        ``agents/task_agent_lite.py``) rather than reading ``orchestrator._chat_session_key``
        directly, so this also works after an orchestrator recreation.
        """
        try:
            if getattr(self.state, "n_steps", 0) != 1:
                return
            if getattr(self, "_session_bootstrap_done", False):
                return
            if getattr(self, "_is_sub_agent", False):
                return
            from agents.task.goals.autonomy_marker import is_autonomous
            if is_autonomous(self.session_id):
                return
            from agents.task.constants import AutonomyConfig
            if not AutonomyConfig.continuity_bridge_enabled():
                return

            thread_key = None
            container = getattr(self, "container", None)
            if container is not None:
                registry = container.get_service("session_chat_registry")
                if registry is not None and hasattr(registry, "resolve_by_session_id"):
                    row = registry.resolve_by_session_id(self.session_id)
                    if row:
                        thread_key = row.get("session_key")
            if not thread_key:
                return

            from core.surfaces.continuity import build_bridge_message
            msg = await build_bridge_message(
                user_id=getattr(self, "user_id", None), thread_key=thread_key)
            if msg is not None:
                self.message_manager.push_ephemeral_message(msg)
                self.logger.debug("Injected continuity bridge message")
        except Exception as e:
            self.logger.debug(f"continuity bridge injection skipped: {e}")
