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

_SUMMARY_MAX_CHARS = 500


def _derive_closing_chat_summary(orchestrator) -> Optional[str]:
    """Best-effort short H-MEM summary of the CLOSING chat session (Task 6 Part A).

    Sourced from the (non-sub-agent) session's ``ContextRetriever._format_session_summary()``
    — the same Layer-1 "[HIERARCHICAL MEMORY - SESSION CONTEXT]" text the retriever
    already builds for in-session context injection, reached via
    ``agent.task_context_manager.get_session(session_id).context_retriever``.

    Gated by ``AutonomyConfig.continuity_bridge_enabled()`` at the call site (this
    function is only invoked when the flag is on) so behaviour when the bridge is
    disabled is byte-identical to before Task 6 (summary stays None).

    Fail-open: ANY error (missing attribute, no H-MEM session, provider hiccup) is
    swallowed and returns None — a summary failure must never break cleanup.
    """
    try:
        agent = None
        for candidate in getattr(orchestrator, "agents", {}).values():
            if not getattr(candidate, "_is_sub_agent", False):
                agent = candidate
                break
        if agent is None:
            return None

        task_context_manager = getattr(agent, "task_context_manager", None)
        if task_context_manager is None:
            return None

        session_id = getattr(orchestrator, "session_id", None)
        session_data = task_context_manager.get_session(session_id)
        retriever = getattr(session_data, "context_retriever", None) if session_data else None
        if retriever is None:
            return None

        text = retriever._format_session_summary()
        text = (text or "").strip()
        return text[:_SUMMARY_MAX_CHARS] or None
    except Exception:
        logger.warning("continuity bridge summary derivation failed", exc_info=True)
        return None


class SessionCleanupMixin:
    """Session teardown for SessionOrchestrator."""

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
            # Episodic-memory write: one row per genuine CHAT session completion.
            # Goal/cron runs already record their OWN episode at the dispatcher/
            # runner site — they're marked via autonomy_marker.mark_autonomous, so
            # skip them here to avoid a double-recorded episode for the same run.
            # Fail-open: a memory error must NEVER block session teardown.
            try:
                from agents.task.goals.autonomy_marker import is_autonomous
                _episode_session_id = getattr(self, "session_id", None)
                if not is_autonomous(_episode_session_id):
                    outcome = _CLEANUP_STATUS_TO_OUTCOME.get((status or "").lower())
                    if outcome is not None:
                        from modules.memory.episodic import finalize_episode
                        # Task 6 Part A: best-effort H-MEM summary of the closing
                        # session, ONLY when the continuity bridge is enabled (keeps
                        # the flag-off path byte-identical: summary stays None).
                        _summary = None
                        try:
                            from agents.task.constants import AutonomyConfig
                            if AutonomyConfig.continuity_bridge_enabled():
                                _summary = _derive_closing_chat_summary(self)
                        except Exception:
                            _summary = None
                        await finalize_episode(
                            session_id=_episode_session_id,
                            user_id=getattr(self, "user_id", None),
                            kind="chat",
                            outcome=outcome,
                            summary=_summary,
                            thread_key=getattr(self, "_chat_session_key", None),
                            meta={"source": "session_end", "status": status},
                        )
            except Exception:
                self.logger.warning("chat episodic write failed", exc_info=True)

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
                                closed = agent.task_context_manager.close_session(
                                    session_id=self.session_id,
                                    user_id=self.user_id
                                )
                                if closed:
                                    self.logger.info(f"💾 Saved + released hierarchical memory for agent {agent_id}")
                                else:
                                    self.logger.warning(f"⚠️ No H-MEM session to close for agent {agent_id}")
                            except Exception as hmem_error:
                                self.logger.error(f"Failed to close H-MEM for agent {agent_id}: {hmem_error}")
                    except Exception as e:
                        self.logger.error(f"Failed to save state for agent {agent_id}: {e}")

                # Close all agents
                for agent_id, agent in list(self.agents.items()):
                    try:
                        if hasattr(agent, 'cleanup'):
                            await agent.cleanup()
                        self.logger.debug(f"Cleaned up agent {agent_id}")
                    except Exception as e:
                        self.logger.error(f"Error cleaning up agent {agent_id}: {e}")

                # M2 FIX: close per-session ISOLATED aux/reflection LLM clients. These are
                # built fresh (not cached, not in self.llm_clients) by
                # _provision_aux_llm(isolated=True) for compaction/reflection, so nothing
                # else closes them — their httpx connection pools would leak per session
                # whenever COMPACTION_MODEL / AUX_* / REFLECTION_LLM_ENABLED is set.
                for agent_id, agent in list(self.agents.items()):
                    for owner, attr in (
                        (getattr(agent, 'message_manager', None), 'aux_llm'),
                        (getattr(agent, 'task_context_manager', None), 'reflection_llm'),
                    ):
                        if owner is None:
                            continue
                        aux = getattr(owner, attr, None)
                        if aux is None:
                            continue
                        # aux is a BaseChatModel adapter wrapping the real client in ._client
                        underlying = getattr(aux, '_client', aux)
                        try:
                            cleanup_fn = getattr(underlying, 'cleanup', None) or getattr(underlying, 'aclose', None)
                            if cleanup_fn is not None:
                                import asyncio
                                if asyncio.iscoroutinefunction(cleanup_fn):
                                    await cleanup_fn()
                                else:
                                    cleanup_fn()
                                self.logger.debug(f"✓ Closed isolated {attr} for agent {agent_id}")
                        except Exception as e:
                            self.logger.warning(f"Error closing isolated {attr} for {agent_id}: {e}")
                        finally:
                            try:
                                setattr(owner, attr, None)
                            except Exception:
                                pass

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
                        if mcp_tool and hasattr(mcp_tool, 'unload_user_servers'):
                            # C6: unload only THIS session's tenant servers, explicitly.
                            # Do NOT set_user_context(None) here — on the shared singleton
                            # that would null the context out from under a concurrent
                            # session; the per-action path re-sets it before every action.
                            unloaded_count = await mcp_tool.unload_user_servers(user_id=self.user_id)
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

                # Flush telemetry
                if hasattr(self, 'telemetry_manager') and self.telemetry_manager:
                    try:
                        await self.telemetry_manager.flush_buffers()
                        self.logger.debug("Flushed telemetry buffers")
                    except Exception as e:
                        self.logger.debug(f"Error flushing telemetry: {e}")

                # Release controller tools with comprehensive cleanup
                if self.controller:
                    try:
                        cleaned_count = 0
                        failed_count = 0

                        for tool_name in self.controller.list_tools():
                            tool = self.controller.get_tool(tool_name)
                            if not tool:
                                continue

                            try:
                                # Try multiple cleanup patterns
                                cleaned = False

                                # Method 1: _cleanup() (preferred async)
                                if hasattr(tool, '_cleanup') and callable(tool._cleanup):
                                    import asyncio
                                    if asyncio.iscoroutinefunction(tool._cleanup):
                                        await tool._cleanup()
                                    else:
                                        tool._cleanup()
                                    cleaned = True

                                # Method 2: cleanup() (alternative)
                                elif hasattr(tool, 'cleanup') and callable(tool.cleanup):
                                    import asyncio
                                    if asyncio.iscoroutinefunction(tool.cleanup):
                                        await tool.cleanup()
                                    else:
                                        tool.cleanup()
                                    cleaned = True

                                # Method 3: close() (HTTP clients, etc.)
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

                        if cleaned_count > 0 or failed_count > 0:
                            self.logger.info(
                                f"Cleaned up {cleaned_count} tool(s)"
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

            # Update session status if provided and not overwriting terminal states
            # DEFENSIVE: Don't overwrite terminal statuses (error, failed, cancelled) with non-terminal
            # Terminal states should only be changed by explicit user action or retry logic
            TERMINAL_STATUSES = {'error', 'failed', 'cancelled'}
            
            if self.session_manager and status:
                current_status = self.session_manager.get_session_info(self.session_id)
                if current_status:
                    current_status_value = current_status.get('status', '').lower()
                    
                    # Protect terminal statuses from being overwritten with non-terminal status
                    if current_status_value in TERMINAL_STATUSES and status.lower() not in TERMINAL_STATUSES:
                        self.logger.debug(
                            f"Preserved terminal status '{current_status_value}', "
                            f"not overwriting with: {status}"
                        )
                    else:
                        self.session_manager.update_session_status(self.session_id, status)
                        self.logger.debug(f"Updated session status to: {status}")
                else:
                    self.session_manager.update_session_status(self.session_id, status)

            cleanup_type = "full" if full_cleanup else "partial"
            self.logger.info(f"Session {self.session_id} {cleanup_type} cleanup complete, status: {status}")

        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")

