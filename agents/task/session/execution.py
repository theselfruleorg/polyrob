"""Agent-creation + session-execution mixin (roadmap P9; code-motion from orchestrator.py).

create_agent (build + register an Agent) and execute_session (run the session loop),
extracted as whole methods so orchestrator.py drops under 700L. SessionOrchestrator
composes SessionExecutionMixin; callers (task_agent_lite, sub_agent_manager) use
orchestrator.create_agent / execute_session unchanged via MRO.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

# Module-level Agent import (no cycle: service does not import orchestrator, and
# service is loaded before this module during orchestrator import).
from agents.task.agent.service import Agent
from core.exceptions import LLMError, LLMPermanentError, LLMProviderExhaustedError


def _result_session_status(result) -> str:
    """Map an agent.run() result (AgentHistoryList) to a session status.

    Live-test F7b: a permanent LLM/billing error halts the loop *inside* the agent
    (error_recovery sets a final ActionResult with .error and is_done=True), so
    agent.run() returns a truthy history and the old `"completed" if result` logic
    mislabeled the halt as a success. A terminal result carrying .error is an
    'error', not 'completed'. A genuine done() has error=None, so success is
    unaffected. Falsy result → 'failed' (unchanged).
    """
    if not result:
        return "failed"
    try:
        last = result.history[-1].result[-1]
        if getattr(last, "error", None):
            return "error"
    except (AttributeError, IndexError, TypeError):
        pass
    return "completed"


class SessionExecutionMixin:
    """Agent creation + session execution for SessionOrchestrator."""

    async def create_agent(self,
                          task: str,
                          llm: Any = None,
                          agent_name: str = "agent",
                          profile_id: Optional[str] = None,
                          profile_overrides: Optional[Dict[str, Any]] = None,
                          share_controller: bool = True,
                          **kwargs) -> Any:
        """Create an agent for this session.

        Args:
            task: Task description for the agent
            llm: Language model to use
            agent_name: Name for the agent
            profile_id: Optional profile ID to use
            profile_overrides: Optional overrides for the profile
            share_controller: Whether to share the controller with other agents
            **kwargs: Additional agent configuration

        Returns:
            Configured Agent instance
        """
        from agents.task.agent.service import Agent

        self.logger.info(f"Creating agent '{agent_name}' for session {self.session_id}")

        # CRITICAL FIX: If no LLM provided, get default from LLMManager
        # This ensures sub-agents created via SubAgentManager get a valid LLM
        if llm is None:
            self.logger.info(f"No LLM provided for agent '{agent_name}', getting default from LLMManager")
            try:
                if self.container:
                    llm_manager = self.container.get_service('llm')
                    if llm_manager:
                        # Get default LLM using the primary provider
                        # Use fallback method to get any available provider
                        llm = await llm_manager.get_fallback_chat_model(
                            exclude_providers=[],  # Don't exclude any providers
                            temperature=0.0
                        )
                        if llm:
                            self.logger.info(f"✅ Got default LLM for agent '{agent_name}': {getattr(llm, 'model_name', 'unknown')}")
                        else:
                            self.logger.error(f"❌ LLMManager returned no available LLM for agent '{agent_name}'")
                            raise RuntimeError(f"No LLM available for agent '{agent_name}'. Check your API keys.")
                    else:
                        self.logger.error(f"❌ LLMManager not found in container for agent '{agent_name}'")
                        raise RuntimeError(f"LLMManager not available in container for agent '{agent_name}'")
                else:
                    self.logger.error(f"❌ No container available to get LLM for agent '{agent_name}'")
                    raise RuntimeError(f"No container available to get LLM for agent '{agent_name}'")
            except Exception as e:
                self.logger.error(f"Failed to get default LLM for agent '{agent_name}': {e}", exc_info=True)
                raise RuntimeError(f"Cannot create agent '{agent_name}' without LLM: {e}")

        # Generate unique agent ID (matches what Agent class will set)
        agent_id = f"{agent_name}_{self.session_id}"

        # Prepare agent configuration with all required parameters
        # NOTE: Browser context obtained on-demand via orchestrator.get_browser_context()
        agent_config = {
            "agent_name": agent_name,
            "task": task,
            "llm": llm,
            "orchestrator": self,  # Orchestrator provides session_id and user_id (single source of truth)
            **kwargs
        }
        # session_id and user_id removed - Agent gets them from orchestrator

        # Note: Controller is accessed via self.orchestrator.controller in Agent.__init__
        # No need to pass it explicitly as a parameter

        # Add session_config if available
        if hasattr(self, 'session_config') and self.session_config:
            agent_config["session_config"] = self.session_config

        # S1/S2 (chat consolidation): forward the chat-mode persona block set on
        # the orchestrator (by TaskAgent.chat_once) into AgentConfig so the agent's
        # SystemPrompt <identity> carries the character. None => byte-identical.
        _persona = getattr(self, '_persona_block', None)
        if _persona:
            agent_config["persona_block"] = _persona

        # Use profile if specified
        if profile_id:
            agent_config["profile_id"] = profile_id
            if profile_overrides:
                agent_config["profile_overrides"] = profile_overrides

        # Create the agent
        try:
            agent = Agent.from_params(**agent_config)

            # Validate requested tools are available
            if hasattr(self, 'session_config') and self.session_config and isinstance(self.session_config, dict):
                requested_tools = self.session_config.get('tools', [])
                if requested_tools:
                    available_tools = self.controller.list_tools()
                    missing_tools = [t for t in requested_tools if t not in available_tools]
                    if missing_tools:
                        self.logger.warning(f"Agent '{agent_name}' missing requested tools: {missing_tools}")

            # Track the agent
            self.agents[agent_id] = agent
            self.agent_types[agent_id] = profile_id or "executor"
            self.agent_names[agent_id] = agent_name
            import time
            self.agent_creation_times[agent_id] = time.time()
            # Store model name using agent property (after agent is created)
            self.agent_models[agent_id] = agent.model_name

            # Register agent with SessionManager (centralized registration)
            # Skip for sub-agents - they're ephemeral and shouldn't pollute session registry
            is_sub_agent = kwargs.get('is_sub_agent', False)
            if self.session_manager and not is_sub_agent:
                try:
                    self.session_manager.register_agent(
                        session_id=self.session_id,
                        agent_name=agent_name,
                        agent_id=agent_id,
                        agent_type=type(agent).__name__,
                        model_name=agent.model_name,  # Use agent property (delegates to MessageManager)
                        user_id=self.user_id,
                        role="executor"
                    )
                    self.logger.debug(f"Registered agent '{agent_name}' with SessionManager")

                    # Emit telemetry for agent creation using TelemetryManager
                    try:
                        self.telemetry_manager.capture_agent_registration(
                            agent_id=agent_id,
                            agent_name=agent_name,
                            agent_type=type(agent).__name__,
                            model_name=agent.model_name,  # Use agent property (delegates to MessageManager)
                            task=task
                        )
                    except Exception as te:
                        self.logger.debug(f"Failed to emit agent registration telemetry: {te}")
                except Exception as e:
                    self.logger.warning(f"Failed to register agent with SessionManager: {e}")
            elif is_sub_agent:
                self.logger.debug(f"Skipped SessionManager registration for sub-agent '{agent_name}'")

            # Register streaming callback if one was supplied (server layer
            # provides the transport; core mode leaves _on_stream_chunk None).
            if self._on_stream_chunk is not None:
                await self._register_stream_callback(agent)

            # CRITICAL FIX: Flush pending messages to newly created agent
            # This ensures messages queued before agent existed are delivered
            # SECURITY FIX: Use lock to prevent race with submit_user_message
            async with self._pending_messages_lock:
                if self._pending_messages and hasattr(agent, 'hitl_manager') and agent.hitl_manager:
                    pending_count = len(self._pending_messages)
                    image_count = sum(
                        1 for _, _, meta in self._pending_messages
                        if meta and meta.get('image_attachments')
                    )
                    self.logger.info(
                        f"📦 Flushing {pending_count} pending message(s) to agent {agent_id} "
                        f"({image_count} with images)"
                    )
                    for text, kind, metadata in self._pending_messages:
                        await agent.hitl_manager.queue_user_message(text, kind, metadata)
                        has_images = bool(metadata and metadata.get('image_attachments'))
                        self.logger.debug(f"  → Delivered pending message (has_images={has_images})")
                    self._pending_messages.clear()
                    self.logger.info(f"✅ All pending messages delivered to agent")

            self.logger.info(f"✅ Created agent '{agent_name}' with ID {agent_id}")
            return agent

        except Exception as e:
            self.logger.error(f"Failed to create agent '{agent_name}': {e}")
            raise

    async def execute_session(self,
                            agent_sequence: List[str],
                            max_steps_per_agent: Optional[Dict[str, int]] = None,
                            shared_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute a session with the given agent sequence.

        Args:
            agent_sequence: List of agent IDs to execute in sequence
            max_steps_per_agent: Maximum steps per agent
            shared_context: Shared context data

        Returns:
            Results from the session execution
        """
        self.logger.info(f"Executing session with {len(agent_sequence)} agents: {agent_sequence}")

        # C-N1: fire on_session_start once (fail-open; no-op unless hooks registered).
        if not getattr(self, "_session_start_fired", False):
            self._session_start_fired = True
            run_hooks = getattr(self, "_run_session_start_hooks", None)
            if run_hooks is not None:
                await run_hooks(session_id=self.session_id, user_id=getattr(self, "user_id", None))

        results = {}
        max_steps_per_agent = max_steps_per_agent or {}

        try:
            for agent_id in agent_sequence:
                if agent_id not in self.agents:
                    self.logger.error(f"Agent {agent_id} not found in session")
                    continue

                agent = self.agents[agent_id]
                max_steps = max_steps_per_agent.get(agent_id, 50)

                self.logger.info(f"Running agent {agent_id} with max {max_steps} steps")

                # Track agent execution
                import time
                start_time = time.time()

                try:
                    # Run the agent
                    result = await agent.run(max_steps=max_steps)
                    end_time = time.time()

                    # Track execution - include agent_type parameter
                    agent_type = type(agent).__name__
                    self.track_agent_execution(agent_id, agent_type, start_time, end_time)

                    # Store result
                    results[agent_id] = {
                        "status": _result_session_status(result),
                        "result": result,
                        "execution_time": end_time - start_time
                    }

                    self.logger.info(f"Agent {agent_id} completed with status: {results[agent_id]['status']}")

                except LLMProviderExhaustedError as e:
                    # All LLM providers have been tried and failed - don't retry
                    end_time = time.time()
                    providers = getattr(e, 'providers_tried', [])
                    self.logger.error(f"❌ Agent {agent_id} exhausted all LLM providers: {providers}")
                    results[agent_id] = {
                        "status": "error",
                        "error": f"All LLM providers exhausted: {providers}",
                        "execution_time": end_time - start_time,
                        "providers_tried": providers
                    }
                
                except LLMPermanentError as e:
                    # Permanent error (auth, billing) - don't retry
                    end_time = time.time()
                    self.logger.error(f"❌ Agent {agent_id} hit permanent LLM error: {e}")
                    results[agent_id] = {
                        "status": "error",
                        "error": f"Permanent LLM error: {str(e)[:300]}",
                        "execution_time": end_time - start_time,
                        "is_permanent": True
                    }
                
                except InterruptedError as e:
                    # CRITICAL FIX (Dec 2025): Handle pause/stop gracefully
                    # InterruptedError is raised when agent is stopped (not paused - pause now waits)
                    end_time = time.time()
                    error_str = str(e).lower()
                    
                    if "paused" in error_str:
                        # Agent was paused - this is a graceful interruption, not an error
                        self.logger.info(f"⏸️ Agent {agent_id} paused by user")
                        results[agent_id] = {
                            "status": "paused",
                            "execution_time": end_time - start_time
                        }
                    else:
                        # Agent was stopped
                        self.logger.info(f"⏹️ Agent {agent_id} stopped: {e}")
                        results[agent_id] = {
                            "status": "stopped",
                            "execution_time": end_time - start_time
                        }
                
                except Exception as e:
                    end_time = time.time()
                    error_str = str(e).lower()

                    # UPGRADE (Dec 2025): Check if this is an LLM error that might benefit from fallback
                    is_llm_error = (
                        isinstance(e, LLMError) or
                        any(x in error_str for x in [
                            'llm', 'rate_limit', 'rate limit', '429', 'authentication',
                            'connection', 'timeout', 'quota'
                        ])
                    )

                    if is_llm_error and hasattr(agent, 'state') and not agent.state.should_halt_for_llm_error():
                        self.logger.warning(f"Agent {agent_id} failed with LLM error, checking for fallback...")

                        # Try to get fallback LLM from container
                        try:
                            if self.container:
                                llm_manager = self.container.get_service('llm')
                                if llm_manager and hasattr(llm_manager, 'get_fallback_chat_model'):
                                    fallback_llm = await llm_manager.get_fallback_chat_model(
                                        exclude_providers=getattr(agent.state, 'llm_providers_failed', [])
                                    )

                                    if fallback_llm:
                                        self.logger.info(f"🔄 Retrying agent {agent_id} with fallback LLM")
                                        agent.llm = fallback_llm
                                        agent.model_name = getattr(fallback_llm, 'model_name', 'fallback')

                                        # Retry execution
                                        retry_start = time.time()
                                        result = await agent.run(max_steps=max_steps)
                                        retry_end = time.time()

                                        results[agent_id] = {
                                            "status": _result_session_status(result),
                                            "result": result,
                                            "execution_time": retry_end - start_time,
                                            "fallback_used": True
                                        }
                                        self.logger.info(f"✅ Agent {agent_id} succeeded with fallback LLM")
                                        continue  # Skip to next agent
                        except LLMProviderExhaustedError:
                            self.logger.error(f"Fallback retry exhausted all providers for {agent_id}")
                        except Exception as fallback_error:
                            self.logger.error(f"Fallback retry failed for {agent_id}: {fallback_error}")

                    self.logger.error(f"Agent {agent_id} failed: {e}")
                    results[agent_id] = {
                        "status": "error",
                        "error": str(e),
                        "execution_time": end_time - start_time
                    }
                finally:
                    # Release browser context immediately after agent completes
                    if self.browser_manager:
                        try:
                            await self.browser_manager.release_context(agent_id, close=True)
                            self.logger.debug(f"Released browser context for completed agent {agent_id}")
                        except Exception as release_error:
                            self.logger.warning(f"Error releasing context for {agent_id}: {release_error}")

            # NOTE: Status updates removed - task_agent_lite owns session status lifecycle
            # Orchestrator only executes agents, it doesn't manage session status
            self.logger.info(f"Session execution completed. Results: {list(results.keys())}")
            return results

        except Exception as e:
            self.logger.error(f"Session execution failed: {e}")
            # NOTE: Don't update status here - let caller (task_agent_lite) handle it
            raise


