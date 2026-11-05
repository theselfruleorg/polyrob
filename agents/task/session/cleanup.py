"""Session cleanup mixin (roadmap P9; code-motion from orchestrator.py).

The session teardown method (browser release, agent/tool cleanup, status update),
split out of SessionOrchestrator as a whole cohesive method so orchestrator.py
shrinks. SessionOrchestrator composes SessionCleanupMixin; callers
(task_agent_lite, tests) use orchestrator.cleanup() unchanged via MRO.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from agents.task.path import pm
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# cleanup(status=...) is called with free-form strings from a handful of production
# sites (task_agent_lite.py's TTL/LRU eviction passes "suspended"; app-shutdown
# reap passes the default "completed"). Map the ones we've actually seen to the
# small terminal vocabulary finalize_episode's outcome accepts
# ({"done","failed","partial","cancelled"}); anything unrecognized is dropped
# (no episode written) rather than guessed at.
_CLEANUP_STATUS_TO_OUTCOME = {
    "completed": "done",
    "suspended": "partial",   # paged-out/evicted, not a genuine completion
    "error": "failed",
    "failed": "failed",
    "cancelled": "cancelled",
}

#: The one key above that is NOT the caller's account of the turn. Eviction passes
#: it for every session it pages out — the one that ended mid-thought and the one
#: whose agent had already called ``done()`` — so on its own it cannot tell them
#: apart. ``_resolve_chat_outcome`` asks the closing turn instead.
_EVICTION_STATUS = "suspended"

_SUMMARY_MAX_CHARS = 500

#: Lines of ``ContextRetriever._format_session_summary()`` that carry no account of
#: what happened: the banner, the session id (the episode row's own column) and the
#: memory machinery's current phase. ``Task:`` and ``Progress:`` are kept — they are
#: the two lines that say anything about the conversation. Truncating the raw block
#: to 500 chars was almost entirely header — intel, 2026-09-16: episode 1230 recorded
#: a fully successful chat as boilerplate, so every consumer (digest, doctor, the
#: oversight loop) under-read that turn.
_HMEM_BOILERPLATE_PREFIXES = (
    "[HIERARCHICAL MEMORY",
    "Session:",
    "Current Phase:",
)


def _strip_hmem_boilerplate(text: Optional[str]) -> Optional[str]:
    """Drop the H-MEM banner/field lines, keeping whatever describes the session.

    Pure. Returns None when nothing substantive survives — an empty summary is an
    honest "no account of this session", which a consumer can render as such; a
    banner is a confident-looking non-answer.
    """
    if not text:
        return None
    kept = []
    for line in str(text).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_HMEM_BOILERPLATE_PREFIXES):
            continue
        kept.append(stripped)
    if not kept:
        return None
    # "Progress: …" is the one field of the block that reports what happened; keep
    # its content without the label so the summary reads as prose.
    if len(kept) == 1 and kept[0].startswith("Progress:"):
        body = kept[0][len("Progress:"):].strip()
        return body[:_SUMMARY_MAX_CHARS] or None
    return "\n".join(kept)[:_SUMMARY_MAX_CHARS] or None


def _primary_agent(orchestrator):
    """The session's own agent — the first non-sub-agent, or None.

    A delegated sub-agent's history is its errand, not the conversation, so both
    the summary and the outcome read this one. Best-effort: an orchestrator
    without an ``agents`` mapping yields None rather than raising in teardown.
    """
    try:
        for candidate in (getattr(orchestrator, "agents", None) or {}).values():
            if not getattr(candidate, "_is_sub_agent", False):
                return candidate
    except Exception:
        return None
    return None


def _agent_history(agent):
    """The agent's step ledger (``AgentHistoryList``).

    It lives on ``agent.history`` (``construction.py``); ``AgentState`` has NO
    history field, so the old ``agent.state.history`` read always came back None
    and the bridge never carried the closing reply (2026-10-13 review C2). The
    ``state.history`` fallback keeps a duck-typed holder working.
    """
    if agent is None:
        return None
    history = getattr(agent, "history", None)
    if history is None:
        history = getattr(getattr(agent, "state", None), "history", None)
    return history


def _closing_turn_called_done(agent) -> bool:
    """Whether the closing turn ended in a ``done()`` result.

    ``AgentHistoryList.is_done()`` is the last step's ``is_done`` flag — the same
    history the cron/goal episode paths already trust. Fail-CLOSED: a missing
    history, a non-callable attribute or a raising accessor all read as "no
    evidence it finished", which is the honest reading and the one that leaves the
    recorded outcome unchanged.
    """
    try:
        history = _agent_history(agent)
        is_done = getattr(history, "is_done", None)
        return bool(is_done()) if callable(is_done) else False
    except Exception:
        return False


def _resolve_chat_outcome(status, orchestrator) -> Optional[str]:
    """The episode outcome for a closing chat session.

    ``status`` is the caller's word and is taken at face value for every value
    except ``_EVICTION_STATUS``, which says only that the session was paged out.
    For that one, a closing turn that called ``done()`` UPGRADES ``partial`` to
    ``done``; anything else leaves it alone.

    Deliberately one-directional. ``has_errors()`` is not consulted, because
    ``errors()`` spans the whole history and a run that recovered from a tool error
    and then finished properly is done — downgrading it would swap one wrong label
    for another. An unrecognised status still returns None (write no episode rather
    than guess), exactly as before.
    """
    outcome = _CLEANUP_STATUS_TO_OUTCOME.get((status or "").lower())
    if outcome is None or (status or "").lower() != _EVICTION_STATUS:
        return outcome
    if _closing_turn_called_done(_primary_agent(orchestrator)):
        return "done"
    return outcome


def _final_assistant_text(agent) -> Optional[str]:
    """The closing turn's own words — the ``done()`` payload or last result.

    ``AgentHistoryList.final_result()`` is the last step's ``extracted_content``,
    which is exactly what the cron/goal episode paths already record. Best-effort:
    a missing history, a non-callable attribute or a raising accessor all degrade
    to None rather than raising inside teardown.
    """
    try:
        history = _agent_history(agent)
        final = getattr(history, "final_result", None)
        if not callable(final):
            return None
        text = (final() or "").strip()
        return text[:_SUMMARY_MAX_CHARS] or None
    except Exception:
        return None


def _derive_closing_chat_summary(orchestrator) -> Optional[str]:
    """Best-effort short summary of the CLOSING chat session (Task 6 Part A).

    Preference order, matching what the cron/goal episode paths record:

    1. The final assistant turn (``agent.history.final_result()`` — the
       ``done()`` payload or the last result), i.e. the session's own account of
       itself.
    2. Failing that, the (non-sub-agent) session's
       ``ContextRetriever._format_session_summary()`` with its banner and
       already-stored fields stripped (``_strip_hmem_boilerplate``), reached via
       ``agent.task_context_manager.get_session(session_id).context_retriever``.

    Before 2026-09-22 only (2) existed and it was recorded RAW, so a successful
    chat's summary was the "[HIERARCHICAL MEMORY - SESSION CONTEXT]" header plus a
    couple of fields the row already carries.

    Gated by ``AutonomyConfig.continuity_bridge_enabled()`` at the call site (this
    function is only invoked when the flag is on) so behaviour when the bridge is
    disabled is byte-identical to before Task 6 (summary stays None).

    Fail-open: ANY error (missing attribute, no H-MEM session, provider hiccup) is
    swallowed and returns None — a summary failure must never break cleanup.
    """
    try:
        agent = _primary_agent(orchestrator)
        if agent is None:
            return None

        spoken = _final_assistant_text(agent)
        if spoken:
            return spoken

        task_context_manager = getattr(agent, "task_context_manager", None)
        if task_context_manager is None:
            return None

        session_id = getattr(orchestrator, "session_id", None)
        session_data = task_context_manager.get_session(session_id)
        retriever = getattr(session_data, "context_retriever", None) if session_data else None
        if retriever is None:
            return None

        return _strip_hmem_boilerplate(retriever._format_session_summary())
    except Exception:
        logger.warning("continuity bridge summary derivation failed", exc_info=True)
        return None


class SessionCleanupMixin:
    """Session teardown for SessionOrchestrator."""

    async def release_shell_sandbox(self, *, generation: Optional[int] = None) -> None:
        """Release this session's pooled persistent shell sandbox container.

        Callable outside full cleanup: autonomous (goal/cron) runs invoke it at
        run end via ``run_task_to_outcome`` — their sessions only ever get the
        PARTIAL cleanup (which keeps the container for continuous chat), so
        without this the container leaked until the next cold-start
        ``reap_orphans`` (live 2026-08-16: 7 containers, 3–21h old). Best-effort
        and idempotent; a later re-entry that runs shell gets a fresh container.

        NOTE: this mixin owns the one allowlisted agents→tools.shell edge
        (tests/test_layering_ratchet.py) — callers must route through here,
        not import backend_pool themselves.
        """
        try:
            from tools.shell.backend_pool import teardown_session as _shell_teardown
            sid = getattr(self, "session_id", None)
            await (_shell_teardown(sid) if generation is None
                   else _shell_teardown(sid, generation=generation))
        except Exception:
            pass

    def shell_sandbox_generation(self) -> Optional[int]:
        """098 review: the run generation of this session's sandbox — a release
        that names it never tears down (or closes) a LATER run's sandbox."""
        try:
            from tools.shell.backend_pool import session_generation
            return session_generation(getattr(self, "session_id", None))
        except Exception:
            return None

    def reopen_shell_sandbox(self) -> None:
        """098 TS-09: the session resumed — clear the closed marker its last
        sandbox teardown set, so a shell call gets a fresh container (until
        then a late call is refused instead of starting one). Fail-open."""
        try:
            from tools.shell.backend_pool import reopen_session
            reopen_session(getattr(self, "session_id", None))
        except Exception:
            pass

    def _release_session_maps(self) -> None:
        """Drop this session's rows from the process-wide maps (102 3.5), last, so no
        event of the cleanup writes one again. Each one reloads on a resume: todos from
        todo.md, the feed sequence from the feed folder, the model pin from the record."""
        from agents.task.model_pin_state import forget_session
        from agents.task.telemetry.sequence import SequenceGenerator
        task_tool = self.controller.get_tool('task') if getattr(self, "controller", None) else None
        release_todos = getattr(task_tool, "release_session", None)
        if callable(release_todos):
            release_todos(self.session_id)
        SequenceGenerator.reset(self.session_id)
        forget_session(self.session_id)

    def _is_shared_tool_instance(self, tool_name: str, tool) -> bool:
        """True if ``tool`` is a process-wide shared instance this session must
        NOT tear down: the DependencyContainer's registered service (identity
        check against both lookup names load_tools_from_container uses), or
        the shared BrowserManager's browser.

        Fail-open toward "session-owned" only when there is genuinely no
        container to consult — a wrong "shared" answer merely leaks a
        session-local tool; a wrong "owned" answer kills a singleton for the
        whole process (the 2026-08-21 outage).
        """
        container = getattr(self, "container", None)
        if container is not None and hasattr(container, "get_service"):
            for candidate in (f"{tool_name}_tool", tool_name):
                try:
                    if container.has_service(candidate) and container.get_service(candidate) is tool:
                        return True
                except Exception:
                    continue
        browser_manager = getattr(self, "browser_manager", None)
        if browser_manager is not None and getattr(browser_manager, "browser", None) is tool:
            return True
        return False

    async def write_chat_episode(self, *, status: str, source: str,
                                 use_llm: bool = True) -> bool:
        """Write this OWNER chat session's episode — one row per session, upserted.

        Called at EVERY boundary that ends an owner chat (F5, prod review
        2026-10-29): full cleanup / eviction (``source="session_end"``), ``/new``
        (``"new"``), the idle/daily reset (``"reset"``) and a drained restart
        (``"restart"``). Before, only eviction wrote one, so most sessions left
        the continuity bridge nothing. The summary covers the whole session
        (``agents.task.session.chat_episode.summarize_session``).

        Goal/cron runs record their OWN episode at the dispatcher/runner site;
        a room and a correspondent-facing session write none. A boundary that
        finds nothing new since this session's last write writes nothing.
        Returns True when a row was written. Fail-open: a memory error must
        NEVER block a boundary.
        """
        try:
            from core.autonomy_marker import is_autonomous
            from core.surfaces.room_policy import is_public_session
            _episode_session_id = getattr(self, "session_id", None)
            # 044 C5: a PUBLIC (room) session writes NO episode. The episodic
            # store is owner-tenant state — it is read back by the
            # session-start digest, the continuity bridge and the recap, all
            # of which run in the owner's DM. A room session runs AS the owner
            # tenant (whoever opened it), so without this guard a stranger's
            # room conversation became a row in the owner's own history.
            from agents.task.session_class import regime_policy
            if is_public_session(self):
                self.logger.debug("public (room) session: no episodic write")
                return False
            if not regime_policy(orchestrator=self,
                                 session_id=_episode_session_id).owner_memory:
                # AGT-6 / DATA-9: a session a correspondent drives (created for
                # one, or tainted by one) is not the owner's history either.
                self.logger.debug("correspondent-facing session: no episodic write")
                return False
            if is_autonomous(_episode_session_id):
                return False
            # The eviction status ("suspended") is not an account of the
            # turn — ask the closing turn itself whether it finished.
            outcome = _resolve_chat_outcome(status, self)
            if outcome is None:
                return False
            _agent = _primary_agent(self)
            from agents.task.runtime.run_outcome import absolute_step_count
            _mark = absolute_step_count(_agent_history(_agent))
            if _mark and getattr(self, "_chat_episode_mark", None) == _mark and (
                    getattr(self, "_chat_episode_llm", False) or not use_llm):
                # Nothing happened since this session's last episode (an LLM
                # write may still upgrade an extract written at the same mark).
                return False
            _inflight = self.__dict__.setdefault("_chat_episode_inflight", set())
            if _mark and (_mark, bool(use_llm)) in _inflight:
                return False   # L3: another boundary writes this same account now
            _inflight.add((_mark, bool(use_llm)))
        except Exception:
            self.logger.warning("chat episodic write failed", exc_info=True)
            return False
        try:
            return await self._write_chat_episode_row(
                status=status, source=source, use_llm=use_llm, outcome=outcome,
                mark=_mark, session_id=_episode_session_id)
        finally:
            _inflight.discard((_mark, bool(use_llm)))

    async def _write_chat_episode_row(self, *, status: str, source: str, use_llm: bool,
                                      outcome: str, mark: int, session_id) -> bool:
        """The write itself, for :meth:`write_chat_episode` (which decided it is due)."""
        _mark, _episode_session_id = mark, session_id
        try:
            from modules.memory.episodic import finalize_episode
            # The whole-session summary, ONLY when the continuity bridge is
            # enabled (the flag-off path keeps summary None).
            _summary = None
            try:
                from core.config_policy import AutonomyConfig
                if AutonomyConfig.continuity_bridge_enabled():
                    from agents.task.session.chat_episode import summarize_session
                    _summary = await summarize_session(self, use_llm=use_llm)
            except Exception:
                _summary = None
            # P2-5: chat episodes were written with no task, no spend, and
            # no steps, so the session-start digest rendered every chat row
            # as `- chat:done $0.00 ""`. Pass the main agent's task and
            # collect_provenance. Fail-open.
            _task = None
            try:
                for _a in (getattr(self, "agents", None) or {}).values():
                    _t = getattr(_a, "task", None)
                    if _t:
                        _task = str(_t)[:500]
                        break
            except Exception:
                _task = None
            _prov = {"spend_usd": 0.0, "steps": 0, "artifacts": []}
            try:
                from modules.memory.episodic import collect_provenance
                _prov = await collect_provenance(self)
            except Exception:
                pass
            if (not use_llm and getattr(self, "_chat_episode_mark", None) == _mark
                    and getattr(self, "_chat_episode_llm", False)):
                return False   # an LLM account of this same mark landed meanwhile
            await finalize_episode(
                session_id=_episode_session_id,
                user_id=getattr(self, "user_id", None),
                kind="chat",
                task=_task,
                outcome=outcome,
                summary=_summary,
                spend_usd=_prov.get("spend_usd", 0.0),
                steps=_prov.get("steps", 0),
                artifacts=_prov.get("artifacts", []),
                skills_loaded=_prov.get("skills_loaded"),
                skills_activation=_prov.get("skills_activation"),
                thread_key=getattr(self, "_chat_session_key", None),
                meta={"source": source, "status": status},
            )
            self._chat_episode_mark = _mark
            self._chat_episode_llm = bool(use_llm)
            return True
        except Exception:
            self.logger.warning("chat episodic write failed", exc_info=True)
            return False

    async def cleanup(
        self,
        preserve_workspace: bool = False,
        preserve_agents: bool = True,
        status: str = "completed",
        full_cleanup: bool = False
    ) -> None:
        """Clean up orchestrator resources.

        This method releases resources owned by THIS session only.
        It does NOT cleanup the shared BrowserManager (that's for app shutdown only).

        Args:
            preserve_workspace: Whether to preserve workspace files
            preserve_agents: Whether to keep agents in registry for continuous chat (default True)
            status: Status to mark in metadata (completed, error, cancelled)
            full_cleanup: If True, release ALL resources (agents, LLMs, tools).
                         If False, only release browser contexts (for continuous chat).
        """
        # C-N1: fire on_session_end once, on a full cleanup (fail-open; no-op unless
        # hooks registered). Runs before teardown so hooks can still read session state.
        if full_cleanup and not getattr(self, "_session_end_fired", False):
            self._session_end_fired = True
            run_hooks = getattr(self, "_run_session_end_hooks", None)
            if run_hooks is not None:
                await run_hooks(
                    session_id=getattr(self, "session_id", None),
                    user_id=getattr(self, "user_id", None),
                    status=status,
                )
            # Episodic-memory write: the closing chat episode (the continuity
            # bridge's source). The SAME method runs at every other boundary
            # (/new, the idle/daily reset, a drained restart — F5).
            # L2: at process shutdown (TaskAgent._cleanup sets the flag) the
            # structured extract only — one aux-LLM call per session, in
            # sequence, could outlast the supervisor's stop timeout.
            await self.write_chat_episode(
                status=status, source="session_end",
                use_llm=not getattr(self, "_episode_extract_only", False))

            # Computer-use posture: release this session's persistent shell sandbox
            # container + revoke its published loopback ports (best-effort, no-op
            # unless the shell tool was used at posture>=1). Without this the container
            # only gets swept by reap_orphans (>1h) and its loopback ports linger in
            # the SSRF allowlist. Fail-open — never block teardown.
            await self.release_shell_sandbox()

        try:
            # Release browser contexts for THIS session
            # Use tracked contexts instead of agents dict to prevent leaks
            if self.browser_manager:
                try:
                    released_count = 0
                    failed_count = 0

                    # Get contexts to release from dedicated tracking set
                    contexts_to_release = set()
                    if hasattr(self, '_browser_contexts'):
                        contexts_to_release = self._browser_contexts.copy()
                    else:
                        # Fallback: use agents dict if tracking set not available
                        contexts_to_release = set(self.agents.keys())
                        self.logger.warning("Using fallback agent keys for browser cleanup (tracking set not found)")

                    # Release each tracked browser context
                    for agent_id in contexts_to_release:
                        try:
                            await self.browser_manager.release_context(agent_id, close=True)
                            released_count += 1
                            self.logger.debug(f"Released browser context for {agent_id}")

                            # Remove from tracking set
                            if hasattr(self, '_browser_contexts'):
                                self._browser_contexts.discard(agent_id)

                            # Track context release with telemetry
                            if hasattr(self, 'telemetry_manager') and self.telemetry_manager:
                                try:
                                    self.telemetry_manager.capture_event(
                                        event_type="browser_context_released",
                                        data={
                                            "agent_id": agent_id,
                                            "session_id": self.session_id,
                                            "status": status
                                        }
                                    )
                                except Exception:
                                    pass
                        except Exception as e:
                            failed_count += 1
                            self.logger.error(f"Error releasing context for {agent_id}: {e}")

                    if released_count > 0:
                        self.logger.info(
                            f"Released {released_count} browser context(s) for session {self.session_id}"
                            + (f" ({failed_count} failed)" if failed_count > 0 else "")
                        )

                except Exception as e:
                    self.logger.error(f"Error releasing browser contexts: {e}", exc_info=True)

            # NOTE: Do NOT call browser_manager.cleanup() here!
            # That method closes the ENTIRE shared browser instance and is only
            # meant for application shutdown, not per-session cleanup.

            # On full (permanent) teardown, forget any cached per-agent browser
            # configs for this session. Best-effort over every agent_id this
            # session ever created (self.agents), not just currently-tracked
            # contexts (_browser_contexts only has ones still allocated -- an
            # agent_id whose context was already released in an earlier turn
            # would otherwise never get its config forgotten).
            if full_cleanup and self.browser_manager:
                for agent_id in list(getattr(self, "agents", {}).keys()):
                    try:
                        self.browser_manager.forget_session_config(agent_id)
                    except Exception:
                        pass

            # REMOVED: TodoManager cleanup - now handled by TaskTool
            # TaskTool manages its own cleanup via _cleanup() method

            # If full cleanup, release all resources (for session eviction)
            if full_cleanup:
                self.logger.info(f"Performing full cleanup for session {self.session_id}")

                _reflection_llms = []
                # PERSISTENCE: Save message history and agent state before cleanup
                # Skip sub-agents - they don't need persistence (their results are captured)
                for agent_id, agent in list(self.agents.items()):
                    # Skip sub-agents to prevent context pollution
                    if hasattr(agent, '_is_sub_agent') and agent._is_sub_agent:
                        self.logger.debug(f"Skipping persistence for sub-agent {agent_id}")
                        continue
                        
                    try:
                        # Save message history
                        if hasattr(agent, 'message_manager') and agent.message_manager:
                            agent.message_manager.save_to_disk(
                                session_id=self.session_id,
                                user_id=self.user_id
                            )
                            self.logger.info(f"💾 Saved message history for agent {agent_id}")

                        # Save agent state
                        if hasattr(agent, 'state') and hasattr(agent.state, 'save_to_file'):
                            from agents.task.path import pm
                            state_file = pm().create_file_path(
                                session_id=self.session_id,
                                subdir_name="data",
                                filename="agent_state.json",
                                user_id=self.user_id
                            )
                            if agent.state.save_to_file(state_file):
                                self.logger.info(f"💾 Saved agent state for agent {agent_id}")

                        # Save + release hierarchical memory (H-MEM). M1: close_session
                        # persists to disk AND removes the in-memory HierarchicalMemory
                        # entry — save_session alone left TaskContextManager._sessions (a
                        # process-wide singleton) growing unbounded on a long-running
                        # server / the autonomous goal-cron loop (close_session had no callers).
                        if hasattr(agent, 'task_context_manager') and agent.task_context_manager:
                            try:
                                # P0-7: run close (which may fire a session-close reflection
                                # aux-LLM call) OFF the event loop — synchronously here it
                                # froze the loop up to ~30s (and deadlocked its own metering).
                                # Mirrors the per-step reflection offload (memory_writer.py).
                                import asyncio as _asyncio
                                # 102 M-M: the close clears this slot and may still reflect,
                                # so hold the model here and close it after.
                                _reflection_of = getattr(agent.task_context_manager, "reflection_llm_for", None)
                                if callable(_reflection_of):
                                    _reflection_llms.append(_reflection_of(self.session_id))
                                try:  # 095 W9: the regime may ask for close-time reflection
                                    from agents.task.session_class import regime_policy
                                    _reflect = bool(regime_policy(agent).reflect_on_close)
                                except Exception:
                                    _reflect = False
                                closed, _drained = await _asyncio.to_thread(
                                    agent.task_context_manager.close_session_and_drain,
                                    self.session_id,
                                    self.user_id,
                                    _reflect,
                                )
                                if closed:
                                    self.logger.info(f"💾 Saved + released hierarchical memory for agent {agent_id}")
                                    # P0-7: push the consolidated summary + any unsynced
                                    # findings to the cross-session store (same path the
                                    # per-step loop uses). Previously discarded at close.
                                    from agents.task.session_class import regime_policy
                                    from agents.task.runtime.stop_reason import memory_worthy
                                    _last_stop = getattr(getattr(agent, 'state', None), 'stop_reason', None)
                                    if _drained and not memory_worthy(_last_stop):
                                        # F7: the last run stopped at the step limit / after
                                        # failures / in a loop — its summaries stay in H-MEM.
                                        self.logger.info(
                                            f"close-time memory sync skipped: {len(_drained)} "
                                            f"summary(ies) from a run that stopped ({_last_stop})")
                                    elif _drained and regime_policy(
                                            orchestrator=self,
                                            session_id=getattr(self, "session_id", None)).owner_memory:
                                        try:
                                            from modules.memory.registry import SYNC_INSERTED, memory_sync_turn
                                            _sync = await memory_sync_turn(
                                                getattr(agent, 'task', '') or '',
                                                "\n".join(_drained),
                                                session_id=self.session_id,
                                                user_id=self.user_id,
                                            )
                                            # 025: the close drain is a WRITE site too —
                                            # it rides the session's memory scope (the
                                            # registry resolves it from session_id) and
                                            # now records the memory_write event it lacked.
                                            # 2026-10-13 D4: only a real insert is a write.
                                            from modules.memory.registry import SYNC_FAILED, SYNC_REFUSED_CAP
                                            if _sync in (SYNC_FAILED, SYNC_REFUSED_CAP):
                                                # Not stored. The session is closed, so nothing can
                                                # retry them; the summaries stay only in this
                                                # session's saved H-MEM file.
                                                self.logger.warning(
                                                    f"close-time memory sync not stored ({_sync}): "
                                                    f"{len(_drained)} finding(s) not in cross-session "
                                                    f"memory (kept only in the session's H-MEM file)")
                                            if _sync != SYNC_INSERTED:
                                                raise RuntimeError(f"close-time memory sync: {_sync}")
                                            from agents.task.telemetry.memory_events import emit_memory_event
                                            from modules.memory.scope import session_scope, telemetry_attrs
                                            emit_memory_event(
                                                "memory_write", user_id=self.user_id or "",
                                                session_id=self.session_id, source="session_close",
                                                scope="cross_session", content="\n".join(_drained),
                                                count=len(_drained),
                                                **telemetry_attrs(session_scope(self.session_id)))
                                        except Exception as sync_err:
                                            self.logger.debug(
                                                f"close-time memory sync skipped: {sync_err}"
                                            )
                                else:
                                    self.logger.warning(f"⚠️ No H-MEM session to close for agent {agent_id}")
                            except Exception as hmem_error:
                                self.logger.error(f"Failed to close H-MEM for agent {agent_id}: {hmem_error}")
                    except Exception as e:
                        self.logger.error(f"Failed to save state for agent {agent_id}: {e}")

                # M2 FIX, 102 M-M: close the per-session ISOLATED clients. They are built
                # fresh (not cached, not in self.llm_clients), so nothing else closes them.
                from agents.task.agent.core.model_swap import close_agent_llms, close_own_llm
                _container = getattr(self, "container", None)
                for agent_id, agent in list(self.agents.items()):
                    try:
                        await close_agent_llms(agent, _container)
                        _tcm = getattr(agent, "task_context_manager", None)
                        if _tcm is not None and hasattr(_tcm, "clear_reflection_context"):
                            _tcm.clear_reflection_context(self.session_id)
                    except Exception as e:
                        self.logger.warning(f"Error closing isolated LLM clients for {agent_id}: {e}")
                for _llm in _reflection_llms:
                    await close_own_llm(_llm, _container)

                # Clear message queues and callbacks from HITL managers
                for agent_id, agent in list(self.agents.items()):
                    try:
                        if hasattr(agent, 'hitl_manager') and agent.hitl_manager:
                            agent.hitl_manager.clear_all_queues()
                            agent.hitl_manager.clear_callbacks()
                            self.logger.debug(f"Cleared HITL queues/callbacks for agent {agent_id}")
                    except Exception as e:
                        self.logger.error(f"Error clearing HITL resources for {agent_id}: {e}")

                # Cancel background tasks
                if hasattr(self, '_execution_tasks') and self._execution_tasks:
                    try:
                        for task in self._execution_tasks:
                            if not task.done():
                                task.cancel()
                        self._execution_tasks.clear()
                        self.logger.debug("Cancelled and cleared background tasks")
                    except Exception as e:
                        self.logger.error(f"Error cancelling background tasks: {e}")

                # FIX #14: Clear workspace context cache for this session
                try:
                    from agents.task.workspace_context import get_workspace_context
                    ws_ctx = get_workspace_context()
                    if ws_ctx:
                        ws_ctx.clear_session(self.session_id)
                        self.logger.debug(f"Cleared workspace context cache for session {self.session_id}")
                except Exception as e:
                    self.logger.warning(f"Failed to clear workspace context cache: {e}")

                # FIX: Cleanup user MCP servers loaded for this session
                try:
                    if self.controller:
                        mcp_tool = self.controller.get_tool('mcp')
                        if mcp_tool and hasattr(mcp_tool, 'release_session'):
                            # C6: unload only THIS session's tenant servers, explicitly,
                            # and only when no OTHER live session of the tenant still
                            # holds the shared tool (it would lose them mid-run).
                            # Do NOT set_user_context(None) here — on the shared singleton
                            # that would null the context out from under a concurrent
                            # session; the per-action path re-sets it before every action.
                            unloaded_count = await mcp_tool.release_session(
                                self.controller, self.user_id)
                            if unloaded_count > 0:
                                self.logger.info(f"🔌 Unloaded {unloaded_count} user MCP servers")
                except Exception as e:
                    self.logger.warning(f"Failed to cleanup user MCP servers: {e}")

                # Clear agent references (only if not explicitly preserving them)
                # Note: We're inside the full_cleanup block, so this gives a way to preserve
                # agents even during full cleanup (useful for graceful session handoff)
                if not preserve_agents:
                    agent_count = len(self.agents)
                    self.agents.clear()
                    self.agent_types.clear()
                    self.agent_names.clear()
                    self.agent_models.clear()
                    self.logger.info(f"Cleared {agent_count} agents from orchestrator during full_cleanup")
                else:
                    self.logger.info(
                        f"Preserved {len(self.agents)} agents even during full_cleanup "
                        f"(agent IDs: {list(self.agents.keys())})"
                    )

                # Close LLM clients if available
                if hasattr(self, 'llm_clients') and self.llm_clients:
                    for client_name, client in list(self.llm_clients.items()):
                        try:
                            # Try multiple cleanup methods - different clients use different patterns
                            closed = False

                            # Method 1: async close()
                            if hasattr(client, 'close') and callable(client.close):
                                import asyncio
                                if asyncio.iscoroutinefunction(client.close):
                                    await client.close()
                                else:
                                    client.close()
                                closed = True

                            # Method 2: aclose() (aiohttp pattern)
                            elif hasattr(client, 'aclose') and callable(client.aclose):
                                await client.aclose()
                                closed = True

                            # Method 3: shutdown() (some HTTP clients)
                            elif hasattr(client, 'shutdown') and callable(client.shutdown):
                                await client.shutdown()
                                closed = True

                            # Method 4: Force close underlying HTTP client if accessible
                            elif hasattr(client, 'client') and hasattr(client.client, 'aclose'):
                                await client.client.aclose()
                                closed = True

                            # Method 5: Force close session if accessible
                            elif hasattr(client, 'session') and hasattr(client.session, 'close'):
                                await client.session.close()
                                closed = True

                            if closed:
                                self.logger.debug(f"✓ Closed LLM client {client_name}")
                            else:
                                self.logger.warning(
                                    f"⚠️ LLM client {client_name} has no close method - "
                                    f"connection may leak (type: {type(client).__name__})"
                                )
                        except Exception as e:
                            self.logger.error(f"Error closing LLM client {client_name}: {e}", exc_info=True)
                    self.llm_clients.clear()

                # Flush telemetry and leave the shared service
                if hasattr(self, 'telemetry_manager') and self.telemetry_manager:
                    self.telemetry_manager.release_session()

                # Release SESSION-OWNED controller tools.
                #
                # 2026-08-21 incident: this loop used to call tool._cleanup()
                # on EVERY controller tool — including process-wide container
                # singletons (twitter, mcp, task, ...). One idle session's
                # eviction nulled TwitterTool.client/MCPTool.server_manager
                # for the whole process, and because the direct _cleanup()
                # call skipped cleanup() (the only writer of _initialized),
                # load_tools_from_container's `if not tool.is_initialized`
                # re-init gate never fired again — dead until restart
                # (2026-08-21 singleton-teardown incident: session eviction
                # must not tear down shared container tool singletons).
                #
                # Rules now: (1) a tool instance owned by the container or the
                # shared BrowserManager is NOT this session's to destroy —
                # skip it; (2) session-owned tools are released via the PUBLIC
                # cleanup() (which keeps _initialized honest), falling back to
                # close() — NEVER the private _cleanup().
                if self.controller:
                    try:
                        cleaned_count = 0
                        failed_count = 0
                        skipped_shared = 0

                        for tool_name in self.controller.list_tools():
                            tool = self.controller.get_tool(tool_name)
                            if not tool:
                                continue

                            if self._is_shared_tool_instance(tool_name, tool):
                                skipped_shared += 1
                                self.logger.debug(
                                    f"Skipping shared tool '{tool_name}' — "
                                    f"container/browser-manager owned, not session property"
                                )
                                continue

                            try:
                                cleaned = False

                                # Public cleanup() — the ONLY sanctioned teardown
                                # (updates _initialized so re-init works later).
                                if hasattr(tool, 'cleanup') and callable(tool.cleanup):
                                    import asyncio
                                    if asyncio.iscoroutinefunction(tool.cleanup):
                                        await tool.cleanup()
                                    else:
                                        tool.cleanup()
                                    cleaned = True

                                # close() fallback (HTTP clients, etc.)
                                elif hasattr(tool, 'close') and callable(tool.close):
                                    import asyncio
                                    if asyncio.iscoroutinefunction(tool.close):
                                        await tool.close()
                                    else:
                                        tool.close()
                                    cleaned = True

                                if cleaned:
                                    cleaned_count += 1
                                    self.logger.debug(f"✓ Cleaned up tool: {tool_name}")
                                else:
                                    self.logger.debug(
                                        f"⚠️ Tool {tool_name} has no cleanup method "
                                        f"(type: {type(tool).__name__})"
                                    )
                            except Exception as e:
                                failed_count += 1
                                self.logger.error(f"Error cleaning up tool {tool_name}: {e}")

                        if cleaned_count > 0 or failed_count > 0 or skipped_shared > 0:
                            self.logger.info(
                                f"Cleaned up {cleaned_count} session-owned tool(s), "
                                f"skipped {skipped_shared} shared"
                                + (f" ({failed_count} failed)" if failed_count > 0 else "")
                            )
                    except Exception as e:
                        self.logger.error(f"Error during tool cleanup: {e}")

            # Create a cleanup marker in the workspace if preserving workspace files
            if preserve_workspace:
                try:
                    # Create metadata file to indicate session status
                    import json
                    import time
                    from datetime import datetime
                    import os

                    from agents.task.path import pm
                    metadata_path = pm().create_file_path(self.session_id, ".", ".session_metadata.json")
                    metadata = {
                        "session_id": self.session_id,
                        "user_id": self.user_id,
                        "status": status,
                        "cleanup_time": datetime.now().isoformat(),
                        "timestamp": time.time(),
                        "workspace_preserved": True
                    }
                    
                    with open(metadata_path, "w") as f:
                        json.dump(metadata, f, indent=2)
                    
                    self.logger.info(f"Created workspace metadata file to mark preserved workspace")
                except Exception as e:
                    self.logger.error(f"Error creating workspace metadata: {e}")

            # Update session status only along an edge of the ONE table (098 AG-14,
            # agents/task/agent/session.py SESSION_MACHINE): a cancelled/failed
            # session is never rewritten to 'suspended'/'completed' by a cleanup.
            if self.session_manager and status:
                from agents.task.agent.session import SESSION_MACHINE
                info = self.session_manager.get_session_info(self.session_id)
                cur = SESSION_MACHINE.normalize(str((info or {}).get('status') or '').lower())
                new = SESSION_MACHINE.normalize(status.lower())
                if (not info or cur == new or cur not in SESSION_MACHINE.states
                        or SESSION_MACHINE.allowed(cur, new)):
                    self.session_manager.update_session_status(self.session_id, status)
                    self.logger.debug(f"Updated session status to: {status}")
                else:
                    self.logger.debug(f"Preserved status '{cur}', not overwriting with: {status}")

            if full_cleanup:
                self._release_session_maps()

            cleanup_type = "full" if full_cleanup else "partial"
            self.logger.info(f"Session {self.session_id} {cleanup_type} cleanup complete, status: {status}")

        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")
