"""
Sub-Agent Manager - Comprehensive implementation for subtask delegation.

Pattern: Main agent calls subtask() → spawns sub-agent → collects output.
No inter-agent messaging. Just spawn, run, collect.

CRITICAL: Sub-agents use ISOLATED contexts to prevent session corruption:
- Virtual session IDs (e.g., session_id_sub_1) for message history isolation
- Separate H-MEM namespaces to prevent finding pollution
- Results are collected and formatted, NOT mixed into parent context

UPGRADES (Jan 2026):
- Usage aggregation: Sub-agent usage rolled up to parent session
- Structured output: Preserves JSON data, files, tool results
- Task complexity heuristics: Rejects trivial tasks
- Parent context injection: Sub-agents receive relevant context
- Shared resource coordination: File locks and API rate limiters

Usage:
    # From Controller's subtask action:
    result = await orchestrator.sub_agent_manager.run_subtask(
        task="Research AI trends",
        parent_agent_id="main-agent-123",
        profile_id="executor"
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from agents.task.agent.orchestrator import SessionOrchestrator
    from agents.task.agent.service import Agent


@dataclass
class SubAgentUsage:
    """Usage statistics from a sub-agent execution."""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    credits_charged: int = 0
    api_cost_usd: float = 0.0
    llm_calls: int = 0


@dataclass
class SubAgentOutput:
    """Structured output from a sub-agent execution.

    Preserves data structure instead of converting everything to strings.
    """
    summary: str = ""
    extracted_content: List[Any] = field(default_factory=list)
    files_created: List[str] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    raw_text: str = ""

    def to_string(self, max_length: int = 4000) -> str:
        """Convert to string for backward compatibility."""
        parts = []

        if self.summary:
            parts.append(f"**Summary:** {self.summary}")

        if self.files_created:
            parts.append(f"**Files Created:** {', '.join(self.files_created[:10])}")

        if self.extracted_content:
            # Include first few items of extracted content
            for i, item in enumerate(self.extracted_content[:3]):
                if isinstance(item, dict):
                    parts.append(f"**Data {i+1}:** {json.dumps(item)[:500]}")
                else:
                    parts.append(f"**Content {i+1}:** {str(item)[:500]}")

        if self.raw_text and not parts:
            parts.append(self.raw_text[:max_length])

        result = "\n\n".join(parts)
        return result[:max_length] if len(result) > max_length else result

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'summary': self.summary,
            'extracted_content': self.extracted_content,
            'files_created': self.files_created,
            'tool_results': self.tool_results,
            'raw_text': self.raw_text[:1000] if self.raw_text else ''
        }


@dataclass
class SubAgentResult:
    """Result from a sub-agent execution.

    UPGRADED: Now includes usage tracking and structured output.
    """
    agent_id: str
    task: str
    output: Union[str, SubAgentOutput]  # Can be string (legacy) or structured
    success: bool
    steps_taken: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None
    cancelled: bool = False
    # UPGRADE: Usage tracking
    usage: SubAgentUsage = field(default_factory=SubAgentUsage)
    # UPGRADE: Virtual session ID for aggregation queries
    virtual_session_id: Optional[str] = None

    @property
    def output_text(self) -> str:
        """Get output as string (backward compatible)."""
        if isinstance(self.output, SubAgentOutput):
            return self.output.to_string()
        return str(self.output) if self.output else ""

    @property
    def output_structured(self) -> Optional[SubAgentOutput]:
        """Get structured output if available."""
        if isinstance(self.output, SubAgentOutput):
            return self.output
        return None


class SubAgentManager:
    """
    Manages sub-agent spawning and execution for subtask delegation.

    Simple pattern:
    1. Main agent calls subtask(task, profile)
    2. SubAgentManager spawns a sub-agent with ISOLATED context
    3. Sub-agent runs to completion (or is cancelled on timeout)
    4. Output returned to main agent (NOT mixed into parent context)

    CRITICAL ISOLATION:
    - Sub-agents get virtual session IDs to prevent shared message history
    - Sub-agents get isolated H-MEM namespaces
    - Sub-agent send_message/done actions are intercepted

    LIMITS (Jan 2026):
    - MAX_CONCURRENT_SUB_AGENTS: Prevents too many sub-agents running at once
    - MAX_SUB_AGENT_DEPTH: Prevents sub-agents from spawning more sub-agents

    UPGRADES (Jan 2026):
    - Usage aggregation to parent session
    - Structured output preservation
    - Task complexity validation
    - Parent context injection
    - Shared resource coordination
    """

    # Task patterns that are too simple for sub-agents
    SIMPLE_TASK_PATTERNS = [
        'read file', 'write file', 'save file', 'create file',
        'parse json', 'extract from', 'search for', 'find ',
        'look up', 'check if', 'verify ', 'list ', 'get ',
        'fetch ', 'download ', 'upload ', 'copy ', 'move ',
        'delete ', 'rename ', 'count ', 'calculate '
    ]

    # Minimum task length to be considered complex
    MIN_COMPLEX_TASK_LENGTH = 80

    def __init__(self, orchestrator: 'SessionOrchestrator'):
        """Initialize with parent orchestrator.

        Args:
            orchestrator: Parent SessionOrchestrator that owns this manager
        """
        self.orchestrator = orchestrator
        self.session_id = orchestrator.session_id

        # Logger
        from agents.task.logging_config import get_task_logger
        self.logger = get_task_logger("sub_agent_manager", self.session_id)

        # Track sub-agents: {agent_id: Agent}
        self._sub_agents: Dict[str, 'Agent'] = {}
        self._results: Dict[str, SubAgentResult] = {}

        # Track running tasks for cancellation
        self._running_tasks: Dict[str, asyncio.Task] = {}

        # Track parent-child relationships
        self._parent_map: Dict[str, str] = {}  # child_id -> parent_id

        # Counter for unique sub-agent IDs
        self._counter = 0

        # UPGRADE: Track total usage across all sub-agents for this session
        self._total_usage = SubAgentUsage()

        # FIX (Jan 2026): Semaphore to limit concurrent sub-agents
        from agents.task.constants import TimeoutConfig
        self._concurrency_semaphore = asyncio.Semaphore(TimeoutConfig.MAX_CONCURRENT_SUB_AGENTS)
        self._max_depth = TimeoutConfig.MAX_SUB_AGENT_DEPTH

        self.logger.info(
            f"SubAgentManager initialized (max_concurrent={TimeoutConfig.MAX_CONCURRENT_SUB_AGENTS}, "
            f"max_depth={self._max_depth})"
        )

    def _is_task_too_simple(self, task: str) -> bool:
        """Check if a task is too simple for a sub-agent.

        Sub-agents are expensive (10-60+ API calls). Simple tasks should
        be done directly by the main agent.

        Args:
            task: The task description

        Returns:
            True if task is too simple for a sub-agent
        """
        task_lower = task.lower().strip()

        # Short tasks are usually simple
        if len(task) < self.MIN_COMPLEX_TASK_LENGTH:
            # Check for simple patterns
            for pattern in self.SIMPLE_TASK_PATTERNS:
                if pattern in task_lower:
                    return True

        return False

    def _gather_parent_context(self, parent_agent_id: str) -> str:
        """Gather relevant context from the parent agent.

        Provides sub-agents with context they need to work effectively.

        Args:
            parent_agent_id: The parent agent's ID

        Returns:
            Context string to inject into sub-agent's task
        """
        context_parts = []

        # Find parent agent
        full_parent_id = f"{parent_agent_id}_{self.orchestrator.session_id}"
        parent_agent = self.orchestrator.agents.get(full_parent_id)

        if not parent_agent:
            # Try without session suffix
            parent_agent = self.orchestrator.agents.get(parent_agent_id)

        if parent_agent:
            try:
                # Get parent's recent results
                if hasattr(parent_agent, 'state') and parent_agent.state.last_result:
                    recent_results = []
                    for result in parent_agent.state.last_result[:3]:
                        if hasattr(result, 'extracted_content') and result.extracted_content:
                            content = result.extracted_content
                            if isinstance(content, dict):
                                recent_results.append(json.dumps(content)[:300])
                            else:
                                recent_results.append(str(content)[:300])
                    if recent_results:
                        context_parts.append("**Recent Results:**\n" + "\n".join(recent_results))
            except Exception as e:
                self.logger.debug(f"Failed to get parent results: {e}")

        # Get workspace files
        try:
            workspace = self.orchestrator._path_manager.get_workspace_dir(self.session_id, self.orchestrator.user_id)
            if workspace.exists():
                files = [str(f.relative_to(workspace)) for f in workspace.rglob('*') if f.is_file()][:15]
                if files:
                    context_parts.append(f"**Workspace Files:** {', '.join(files)}")
        except Exception as e:
            self.logger.debug(f"Failed to list workspace: {e}")

        if context_parts:
            return "\n\n## CONTEXT FROM PARENT AGENT\n" + "\n\n".join(context_parts)

        return ""

    async def _aggregate_usage_to_parent(
        self,
        virtual_session_id: str,
        sub_agent_id: str,
        task: str
    ) -> SubAgentUsage:
        """Aggregate sub-agent usage to the parent session.

        Queries the sub-agent's virtual session usage and creates an
        aggregation record in the parent session.

        Args:
            virtual_session_id: The sub-agent's virtual session ID
            sub_agent_id: The sub-agent's ID
            task: The task description

        Returns:
            SubAgentUsage with aggregated statistics
        """
        usage = SubAgentUsage()

        if not hasattr(self.orchestrator, 'usage_tracker') or not self.orchestrator.usage_tracker:
            self.logger.debug("No usage_tracker available for aggregation")
            return usage

        try:
            tracker = self.orchestrator.usage_tracker

            # Query sub-agent's virtual session usage
            sub_usage = await tracker.get_session_breakdown(virtual_session_id)

            if sub_usage and sub_usage.get('total_credits_charged', 0) > 0:
                # Extract usage data
                usage.credits_charged = sub_usage.get('total_credits_charged', 0)
                usage.api_cost_usd = sub_usage.get('total_api_cost_usd', 0.0)

                by_type = sub_usage.get('by_type', [{}])
                if by_type:
                    first = by_type[0]
                    tokens = first.get('tokens', {})
                    usage.input_tokens = tokens.get('input', 0)
                    usage.output_tokens = tokens.get('output', 0)
                    usage.cached_tokens = tokens.get('cached', 0)
                    usage.total_tokens = usage.input_tokens + usage.output_tokens
                    usage.llm_calls = first.get('calls', 0)

                # Record aggregation entry in parent session
                await tracker.db.execute("""
                    INSERT INTO usage_records (
                        user_id, session_id, resource_type,
                        cost, input_tokens, output_tokens, cached_tokens,
                        api_cost_usd, markup_multiplier,
                        metadata, timestamp
                    ) VALUES (?, ?, 'sub_agent_aggregate', ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    self.orchestrator.user_id,
                    self.session_id,  # Parent session!
                    usage.credits_charged,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_tokens,
                    usage.api_cost_usd,
                    1.0,
                    json.dumps({
                        'sub_agent_id': sub_agent_id,
                        'virtual_session_id': virtual_session_id,
                        'task': task[:200],
                        'llm_calls': usage.llm_calls
                    })
                ))

                self.logger.info(
                    f"📊 Aggregated sub-agent usage: {usage.credits_charged} credits, "
                    f"{usage.total_tokens} tokens, ${usage.api_cost_usd:.4f}"
                )

                # Update session total
                self._total_usage.credits_charged += usage.credits_charged
                self._total_usage.input_tokens += usage.input_tokens
                self._total_usage.output_tokens += usage.output_tokens
                self._total_usage.total_tokens += usage.total_tokens
                self._total_usage.api_cost_usd += usage.api_cost_usd
                self._total_usage.llm_calls += usage.llm_calls

        except Exception as e:
            self.logger.warning(f"Failed to aggregate sub-agent usage: {e}")

        return usage
    
    def _generate_sub_agent_id(self, parent_id: str) -> str:
        """Generate unique sub-agent ID.
        
        Args:
            parent_id: Parent agent ID
            
        Returns:
            Unique sub-agent ID like "sub-1-abc123"
        """
        self._counter += 1
        session_prefix = self.session_id[:8] if self.session_id else "unknown"
        return f"sub-{self._counter}-{session_prefix}"
    
    def _generate_virtual_session_id(self, sub_agent_id: str) -> str:
        """Generate a virtual session ID for sub-agent isolation.
        
        This ensures sub-agents don't pollute the parent's:
        - Message history (saved to different path)
        - H-MEM namespace (separate findings)
        
        Args:
            sub_agent_id: The sub-agent's ID
            
        Returns:
            Virtual session ID like "bfb58574_sub_1"
        """
        return f"{self.session_id}__{sub_agent_id}"

    async def _build_child_controller(self):
        """UP-05: build a least-privilege child Controller for a delegated sub-agent.

        Narrows the parent's tool_ids (drops ``code_execution``/``cronjob``) and
        excludes the delegation actions (``subtask``/``parallel_subtasks``/
        ``delegate_task``) for the leaf child via the Registry ``exclude_actions``
        seam — so the child's registry/ActionModel never even contains them (no
        call-time escape, no schema leakage). Heavyweight tools (``browser``/
        ``mcp``) resolve to the SAME container singletons, so no second browser
        context / MCP client is created; the child shares the parent's orchestrator
        (same session_id/user_id) so tool re-configuration writes identical values.

        Returns None when ``SUBAGENT_LEAST_PRIVILEGE`` is off => the caller falls
        back to the shared parent controller (pre-UP-05 behaviour, byte-identical).
        """
        from agents.task.constants import TimeoutConfig
        if not TimeoutConfig.get_subagent_least_privilege():
            return None

        from tools.controller.service import Controller
        from tools.controller.delegation import (
            narrow_child_tools,
            delegation_exclusions_for_child,
            LEAF,
        )

        parent_controller = self.orchestrator.controller
        parent_tools = parent_controller.list_tools()
        # Spawned sub-agents are constructed as 'leaf' (construction.py forces
        # leaf for is_sub_agent), and recursion is independently blocked by the
        # depth gate — so the child cannot delegate. Exclude the delegation actions.
        child_role = LEAF
        child_tool_ids = narrow_child_tools(
            parent_tools=parent_tools,
            requested_tools=None,  # inherit (minus blocklist); future: delegate_task(tools=[...])
            child_role=child_role,
        )
        exclude = sorted(delegation_exclusions_for_child(child_role))

        child_controller = Controller(
            exclude_actions=exclude,
            container=self.orchestrator.container,
            orchestrator=self.orchestrator,
        )
        await child_controller.load_tools_from_container(child_tool_ids)
        self.logger.info(
            f"🔒 Least-privilege child controller: tools={child_controller.list_tools()} "
            f"(excluded actions={exclude})"
        )
        return child_controller

    def _emit_subagent_event(
        self,
        phase: str,
        sub_agent_id: str,
        *,
        goal: str = '',
        child_session_id: str = '',
        ok: bool = False,
        duration: float = 0.0,
    ) -> None:
        """019 P1: emit subagent_started/subagent_finished into the PARENT feed.

        Fail-open, flag-gated — telemetry must never affect the delegation.
        """
        try:
            from core.config_policy import AutonomyConfig
            if not AutonomyConfig.run_events_enabled():
                return
            telemetry = getattr(self.orchestrator, 'telemetry_manager', None)
            if not telemetry:
                return
            from agents.task.telemetry.views import (
                SubagentFinishedEvent,
                SubagentStartedEvent,
            )
            preview = (goal or '')[:120]
            if phase == 'started':
                event = SubagentStartedEvent(
                    agent_id=f"orchestrator_{self.session_id}",
                    child_agent_id=sub_agent_id,
                    child_session_id=child_session_id,
                    goal_preview=preview,
                    session_id=self.session_id,
                )
            else:
                event = SubagentFinishedEvent(
                    agent_id=f"orchestrator_{self.session_id}",
                    child_agent_id=sub_agent_id,
                    ok=ok,
                    duration_seconds=duration,
                    goal_preview=preview,
                    session_id=self.session_id,
                )
            telemetry.capture_event(event)
        except Exception:
            pass

    async def run_subtask(
        self,
        task: str,
        parent_agent_id: str,
        profile_id: str = "executor",
        max_steps: int = 30,
        parent_llm: Any = None,
        is_parent_sub_agent: bool = False,
        skip_complexity_check: bool = False
    ) -> SubAgentResult:
        """
        Spawn and run a sub-agent for a subtask with ISOLATED context.

        This is the main entry point. Blocks until sub-agent completes or times out.

        ISOLATION GUARANTEES:
        - Sub-agent uses virtual session ID for message history
        - Sub-agent H-MEM writes go to separate namespace
        - Sub-agent send_message/done don't pollute parent context

        LIMITS (Jan 2026):
        - Respects MAX_CONCURRENT_SUB_AGENTS via semaphore
        - Respects MAX_SUB_AGENT_DEPTH (sub-agents cannot spawn more sub-agents)

        UPGRADES (Jan 2026):
        - Task complexity validation (rejects trivial tasks)
        - Parent context injection (sub-agents get relevant context)
        - Usage aggregation (sub-agent usage rolled up to parent)
        - Structured output (preserves JSON data)

        Args:
            task: The subtask description
            parent_agent_id: ID of the parent agent delegating this task
            profile_id: Profile to use for sub-agent (default: executor)
            max_steps: Maximum steps for sub-agent
            parent_llm: Optional LLM from parent agent (inherits if not provided)
            is_parent_sub_agent: True if the parent is already a sub-agent
            skip_complexity_check: Skip task complexity validation (for testing)

        Returns:
            SubAgentResult with output, usage, and structured data
        """
        from agents.task.constants import TimeoutConfig

        start_time = time.time()
        sub_agent_id = self._generate_sub_agent_id(parent_agent_id)
        virtual_session_id = self._generate_virtual_session_id(sub_agent_id)

        # UPGRADE: Check if task is too simple for a sub-agent
        if not skip_complexity_check and self._is_task_too_simple(task):
            self.logger.info(
                f"🚫 Task too simple for sub-agent: {task[:60]}... "
                f"(length={len(task)}, matches simple pattern)"
            )
            return SubAgentResult(
                agent_id=sub_agent_id,
                task=task,
                output=f"This task is too simple for a sub-agent. Please complete it directly:\n{task}",
                success=False,
                duration_seconds=time.time() - start_time,
                error="Task too simple - do it directly",
                virtual_session_id=virtual_session_id
            )

        # FIX (Jan 2026): Enforce depth limit - sub-agents cannot spawn more sub-agents
        if is_parent_sub_agent:
            self.logger.warning(
                f"🚫 Depth limit: Sub-agent '{parent_agent_id}' tried to spawn sub-agent. "
                f"Max depth is {self._max_depth}. Task will be executed directly instead."
            )
            return SubAgentResult(
                agent_id=sub_agent_id,
                task=task,
                output=f"Cannot spawn sub-agent: max depth ({self._max_depth}) reached. "
                       f"Please complete this task directly without delegation.",
                success=False,
                duration_seconds=time.time() - start_time,
                error=f"Sub-agent depth limit ({self._max_depth}) exceeded",
                virtual_session_id=virtual_session_id
            )

        self.logger.info(f"🚀 Spawning sub-agent {sub_agent_id} (isolated session: {virtual_session_id[:20]}...)")
        self.logger.info(f"   Task: {task[:100]}...")

        # C-N1: subagent lifecycle start hook (fail-open; no-op unless registered).
        _run_sa_start = getattr(self.orchestrator, "_run_subagent_start_hooks", None)
        if _run_sa_start is not None:
            await _run_sa_start(goal=task, agent_id=sub_agent_id,
                                parent_session_id=self.session_id)
        _sub_ok = False

        # UPGRADE: Gather parent context to inject into sub-agent
        parent_context = self._gather_parent_context(parent_agent_id)
        enriched_task = task
        if parent_context:
            enriched_task = f"{task}\n{parent_context}"
            self.logger.debug(f"Injected {len(parent_context)} chars of parent context")

        # FIX (Jan 2026): Wait for concurrency slot before spawning
        current_running = len(self._running_tasks)
        self.logger.debug(f"Waiting for concurrency slot ({current_running} currently running)")

        # Get parent agent's LLM to pass to sub-agent
        llm_to_use = parent_llm
        if llm_to_use is None:
            # Try multiple lookup patterns for parent agent
            for lookup_id in [parent_agent_id, f"{parent_agent_id}_{self.orchestrator.session_id}"]:
                if lookup_id in self.orchestrator.agents:
                    parent_agent = self.orchestrator.agents[lookup_id]
                    if hasattr(parent_agent, 'llm') and parent_agent.llm is not None:
                        llm_to_use = parent_agent.llm
                        self.logger.debug(f"Sub-agent {sub_agent_id} inheriting LLM from parent")
                        break

        sub_agent = None
        # Use semaphore to limit concurrency
        async with self._concurrency_semaphore:
            self.logger.debug(f"✓ Acquired concurrency slot for {sub_agent_id}")
            try:
                # 019 P1: mirror the child lifecycle into the PARENT session's
                # feed (same-tenant — the child shares the parent orchestrator).
                # Emitted INSIDE this try so the finally's 'finished' emit
                # always pairs it — a cancellation while queued for a slot must
                # not strand the parent's RunActivity at 'delegating'.
                self._emit_subagent_event(
                    'started', sub_agent_id, goal=task,
                    child_session_id=virtual_session_id,
                )
                # UP-05: build a least-privilege child controller (narrowed toolset
                # minus code_execution/cronjob; delegation actions excluded for the
                # leaf child) instead of sharing the parent's full controller. Returns
                # None when SUBAGENT_LEAST_PRIVILEGE is off => shared parent controller.
                child_controller = await self._build_child_controller()

                # Create sub-agent with ISOLATION flags
                sub_agent = await self.orchestrator.create_agent(
                    task=enriched_task,  # UPGRADE: Use enriched task with context
                    agent_name=sub_agent_id,
                    llm=llm_to_use,
                    profile_id=profile_id,
                    share_controller=child_controller is None,  # (param is a no-op; behavior driven by controller= below)
                    controller=child_controller,  # UP-05: least-privilege child controller (None => shared parent controller)
                    # ISOLATION: Mark as sub-agent to prevent context pollution
                    is_sub_agent=True,
                    parent_session_id=self.session_id,  # For reference only
                )

                # CRITICAL: Override session_id on sub-agent's message_manager for isolation
                if hasattr(sub_agent, 'message_manager') and sub_agent.message_manager:
                    sub_agent.message_manager.session_id = virtual_session_id
                    self.logger.debug(f"Isolated message_manager session_id: {virtual_session_id[:20]}...")

                # Track relationships
                self._sub_agents[sub_agent_id] = sub_agent
                self._parent_map[sub_agent_id] = parent_agent_id

                # Run sub-agent with timeout
                timeout = TimeoutConfig.SUB_AGENT_TIMEOUT
                self.logger.info(f"▶️ Running sub-agent {sub_agent_id} (max {max_steps} steps, timeout {timeout}s)")

                try:
                    history = await asyncio.wait_for(
                        sub_agent.run(max_steps=max_steps),
                        timeout=timeout
                    )
                except asyncio.TimeoutError:
                    duration = time.time() - start_time
                    self.logger.warning(f"⏱️ Sub-agent {sub_agent_id} timed out after {duration:.1f}s")

                    # Try to stop the agent gracefully
                    if hasattr(sub_agent, 'stop'):
                        sub_agent.stop()

                    # UPGRADE: Still aggregate usage even on timeout
                    usage = await self._aggregate_usage_to_parent(virtual_session_id, sub_agent_id, task)

                    return SubAgentResult(
                        agent_id=sub_agent_id,
                        task=task,
                        output=f"Sub-agent timed out after {timeout} seconds",
                        success=False,
                        duration_seconds=duration,
                        error=f"Timeout after {timeout}s",
                        cancelled=True,
                        usage=usage,
                        virtual_session_id=virtual_session_id
                    )

                # UPGRADE: Extract structured output (not just string)
                output = self._extract_output_structured(sub_agent, history)

                # UPGRADE: Aggregate usage to parent session
                usage = await self._aggregate_usage_to_parent(virtual_session_id, sub_agent_id, task)

                # Build result
                duration = time.time() - start_time
                result = SubAgentResult(
                    agent_id=sub_agent_id,
                    task=task,
                    output=output,
                    success=True,
                    steps_taken=sub_agent.state.n_steps if hasattr(sub_agent, 'state') else 0,
                    duration_seconds=duration,
                    usage=usage,
                    virtual_session_id=virtual_session_id
                )

                self._results[sub_agent_id] = result
                self.logger.info(
                    f"✅ Sub-agent {sub_agent_id} completed in {duration:.1f}s "
                    f"({result.steps_taken} steps, {usage.credits_charged} credits)"
                )

                _sub_ok = True
                return result

            except asyncio.CancelledError:
                duration = time.time() - start_time
                self.logger.warning(f"🛑 Sub-agent {sub_agent_id} cancelled after {duration:.1f}s")

                # UPGRADE: Aggregate usage even on cancel
                usage = await self._aggregate_usage_to_parent(virtual_session_id, sub_agent_id, task)

                return SubAgentResult(
                    agent_id=sub_agent_id,
                    task=task,
                    output="",
                    success=False,
                    duration_seconds=duration,
                    error="Cancelled",
                    cancelled=True,
                    usage=usage,
                    virtual_session_id=virtual_session_id
                )

            except Exception as e:
                duration = time.time() - start_time
                self.logger.error(f"❌ Sub-agent {sub_agent_id} failed: {e}", exc_info=True)

                # UPGRADE: Aggregate usage even on error
                usage = await self._aggregate_usage_to_parent(virtual_session_id, sub_agent_id, task)

                result = SubAgentResult(
                    agent_id=sub_agent_id,
                    task=task,
                    output="",
                    success=False,
                    duration_seconds=duration,
                    error=str(e),
                    usage=usage,
                    virtual_session_id=virtual_session_id
                )

                self._results[sub_agent_id] = result
                return result

            finally:
                # C-N1: subagent lifecycle end hook (fail-open; no-op unless registered).
                _run_sa_end = getattr(self.orchestrator, "_run_subagent_end_hooks", None)
                if _run_sa_end is not None:
                    await _run_sa_end(goal=task, agent_id=sub_agent_id,
                                      parent_session_id=self.session_id, ok=_sub_ok)
                self._emit_subagent_event(
                    'finished', sub_agent_id, goal=task, ok=_sub_ok,
                    duration=time.time() - start_time,
                )

                # Cleanup sub-agent to prevent resource leaks
                if sub_agent:
                    try:
                        if hasattr(sub_agent, 'cleanup'):
                            await sub_agent.cleanup()
                    except Exception as cleanup_error:
                        self.logger.debug(f"Sub-agent cleanup error: {cleanup_error}")

                    # Remove from orchestrator.agents to prevent accumulation
                    if hasattr(self.orchestrator, 'agents'):
                        # Find the agent_id used in orchestrator.agents (format: agent_name_session_id)
                        agent_id = f"{sub_agent_id}_{self.orchestrator.session_id}"
                        if agent_id in self.orchestrator.agents:
                            del self.orchestrator.agents[agent_id]
                            self.logger.debug(f"Removed sub-agent {agent_id} from orchestrator.agents")
                        # Also clean up tracking dicts
                        for tracking_dict in [
                            self.orchestrator.agent_types,
                            self.orchestrator.agent_names,
                            self.orchestrator.agent_creation_times,
                            self.orchestrator.agent_models
                        ]:
                            if hasattr(tracking_dict, 'pop'):
                                tracking_dict.pop(agent_id, None)

                    # Remove from local tracking
                    self._sub_agents.pop(sub_agent_id, None)
    
    async def run_parallel_subtasks(
        self,
        subtasks: List[Dict[str, Any]],
        parent_agent_id: str,
        max_concurrent: int = 3
    ) -> List[SubAgentResult]:
        """
        Run multiple subtasks in parallel with proper timeout and cancellation.
        
        CRITICAL: This method uses PARALLEL_SUBTASKS_TIMEOUT to ensure all
        sub-agents complete or are cancelled. No zombie sub-agents!
        
        Args:
            subtasks: List of {"task": str, "profile": str} dicts
            parent_agent_id: Parent agent ID
            max_concurrent: Max concurrent sub-agents
            
        Returns:
            List of SubAgentResult objects
        """
        from agents.task.constants import TimeoutConfig
        
        total_timeout = TimeoutConfig.PARALLEL_SUBTASKS_TIMEOUT
        self.logger.info(
            f"🚀 Running {len(subtasks)} subtasks in parallel "
            f"(max {max_concurrent} concurrent, timeout {total_timeout}s)"
        )
        
        # Use semaphore to limit concurrency
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def run_with_semaphore(subtask: Dict, index: int) -> SubAgentResult:
            async with semaphore:
                try:
                    return await self.run_subtask(
                        task=subtask['task'],
                        parent_agent_id=parent_agent_id,
                        profile_id=subtask.get('profile', 'executor'),
                        max_steps=subtask.get('max_steps', 30),
                        # B4: optional per-task LLM (built by the caller); absent/None
                        # is byte-identical to the pre-B4 call (inherits parent LLM).
                        parent_llm=subtask.get('llm')
                    )
                except asyncio.CancelledError:
                    self.logger.warning(f"Subtask {index} cancelled")
                    return SubAgentResult(
                        agent_id=f"cancelled-{index}",
                        task=subtask['task'],
                        output="",
                        success=False,
                        error="Cancelled due to overall timeout",
                        cancelled=True
                    )
        
        # Create tasks so we can cancel them on timeout
        tasks = [
            asyncio.create_task(run_with_semaphore(st, i))
            for i, st in enumerate(subtasks)
        ]
        
        # Track tasks for potential cancellation
        for i, task in enumerate(tasks):
            self._running_tasks[f"parallel_{i}"] = task
        
        try:
            # Wait for all with overall timeout
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=total_timeout
            )
        except asyncio.TimeoutError:
            self.logger.error(f"⏱️ Parallel subtasks timed out after {total_timeout}s - cancelling all")
            
            # Cancel all running tasks
            for task in tasks:
                if not task.done():
                    task.cancel()
            
            # Wait for cancellations to complete (with short timeout)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                self.logger.warning("Some tasks didn't cancel cleanly")
            
            # Build results for completed + cancelled tasks
            results = []
            for i, task in enumerate(tasks):
                if task.done() and not task.cancelled():
                    try:
                        results.append(task.result())
                    except Exception as e:
                        results.append(SubAgentResult(
                            agent_id=f"failed-{i}",
                            task=subtasks[i]['task'],
                            output="",
                            success=False,
                            error=str(e)
                        ))
                else:
                    results.append(SubAgentResult(
                        agent_id=f"timeout-{i}",
                        task=subtasks[i]['task'],
                        output="",
                        success=False,
                        error=f"Cancelled due to {total_timeout}s overall timeout",
                        cancelled=True
                    ))
        finally:
            # Clean up task tracking
            for key in list(self._running_tasks.keys()):
                if key.startswith("parallel_"):
                    del self._running_tasks[key]
        
        # Convert exceptions to failed results
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                final_results.append(SubAgentResult(
                    agent_id=f"failed-{i}",
                    task=subtasks[i]['task'],
                    output="",
                    success=False,
                    error=str(result)
                ))
            else:
                final_results.append(result)
        
        successful = sum(1 for r in final_results if r.success)
        cancelled = sum(1 for r in final_results if r.cancelled)
        self.logger.info(
            f"✅ Parallel execution complete: {successful}/{len(final_results)} succeeded, "
            f"{cancelled} cancelled/timed out"
        )
        
        return final_results
    
    def _extract_output_structured(self, agent: 'Agent', history: Any) -> SubAgentOutput:
        """
        Extract structured output from sub-agent execution.

        UPGRADE: Returns SubAgentOutput instead of string to preserve data structure.

        Args:
            agent: The sub-agent instance
            history: AgentHistoryList from run()

        Returns:
            SubAgentOutput with structured data preserved
        """
        output = SubAgentOutput()

        # PRIORITY 1: Get extracted content from last action results
        if hasattr(agent, 'state') and agent.state.last_result:
            try:
                last_results = agent.state.last_result
                for result in last_results:
                    if hasattr(result, 'extracted_content') and result.extracted_content:
                        # Preserve structure - don't convert to string
                        content = result.extracted_content
                        output.extracted_content.append(content)
            except Exception as e:
                self.logger.debug(f"Failed to extract from last_result: {e}")

        # PRIORITY 2: Get files created in workspace
        try:
            # Use sub-agent's effective session_id for workspace lookup
            effective_session = agent.effective_session_id if hasattr(agent, 'effective_session_id') else agent.session_id
            workspace = self.orchestrator._path_manager.get_workspace_dir(effective_session, agent.user_id if hasattr(agent, 'user_id') else None)
            if workspace.exists():
                files = []
                for f in workspace.rglob('*'):
                    if f.is_file():
                        try:
                            files.append(str(f.relative_to(workspace)))
                        except ValueError:
                            files.append(f.name)
                output.files_created = files[:20]  # Limit to 20 files
        except Exception as e:
            self.logger.debug(f"Failed to list workspace files: {e}")

        # PRIORITY 3: Get tool results from message history
        if hasattr(agent, 'message_manager'):
            try:
                managed_messages = agent.message_manager.history.messages
                for managed_msg in reversed(managed_messages[-15:]):  # Last 15 messages
                    actual_msg = managed_msg.message if hasattr(managed_msg, 'message') else managed_msg
                    msg_type = type(actual_msg).__name__

                    # Collect tool results
                    if 'Tool' in msg_type and hasattr(actual_msg, 'content'):
                        tool_name = getattr(actual_msg, 'name', 'unknown')
                        content = actual_msg.content
                        # Truncate but preserve structure if possible
                        if isinstance(content, dict):
                            output.tool_results.append({
                                'tool': tool_name,
                                'result': content
                            })
                        else:
                            output.tool_results.append({
                                'tool': tool_name,
                                'result': str(content)[:1000]
                            })

                    # Get last AI message as raw text
                    elif ('AI' in msg_type or 'Assistant' in msg_type) and hasattr(actual_msg, 'content'):
                        if not output.raw_text:
                            content = actual_msg.content
                            if isinstance(content, str) and len(content) > 50:
                                output.raw_text = content[:2000]
            except Exception as e:
                self.logger.debug(f"Failed to extract from message_manager: {e}")

        # PRIORITY 4: Get from history if available (AgentHistoryList)
        if history:
            try:
                if hasattr(history, 'extracted_content'):
                    extracted = history.extracted_content()
                    if extracted:
                        for e in extracted[:5]:
                            if e not in output.extracted_content:
                                output.extracted_content.append(e)
            except Exception as e:
                self.logger.debug(f"Failed to extract from history: {e}")

        # Build summary
        if output.extracted_content:
            output.summary = f"Extracted {len(output.extracted_content)} data items"
        elif output.files_created:
            output.summary = f"Created {len(output.files_created)} files: {', '.join(output.files_created[:5])}"
        elif output.tool_results:
            output.summary = f"Executed {len(output.tool_results)} tool calls"
        else:
            output.summary = f"Completed task: {agent.task[:150] if hasattr(agent, 'task') else 'unknown'}"

        return output

    def _extract_output(self, agent: 'Agent', history: Any) -> str:
        """
        Extract output as string (backward compatible).

        Calls _extract_output_structured and converts to string.

        Args:
            agent: The sub-agent instance
            history: AgentHistoryList from run()

        Returns:
            String output summarizing what the agent accomplished
        """
        structured = self._extract_output_structured(agent, history)
        return structured.to_string()
    
    def get_all_results(self) -> List[SubAgentResult]:
        """Get all sub-agent results for this session."""
        return list(self._results.values())
    
    def get_children(self, parent_agent_id: str) -> List[str]:
        """Get child agent IDs for a parent.
        
        Args:
            parent_agent_id: Parent agent ID
            
        Returns:
            List of child agent IDs
        """
        return [
            child_id for child_id, parent_id in self._parent_map.items()
            if parent_id == parent_agent_id
        ]
    
    def format_results_for_prompt(self, results: List[SubAgentResult]) -> str:
        """Format sub-agent results for injection into main agent's prompt.

        UPGRADE: Now includes usage statistics and handles structured output.

        Args:
            results: List of SubAgentResult objects

        Returns:
            Formatted string for prompt injection
        """
        if not results:
            return "No subtask results available."

        formatted = []
        total_credits = 0
        total_duration = 0.0

        for i, result in enumerate(results, 1):
            status = "✅" if result.success else "❌"
            total_credits += result.usage.credits_charged
            total_duration += result.duration_seconds

            # Handle structured output
            if isinstance(result.output, SubAgentOutput):
                output_text = result.output.to_string(max_length=2000)
                # Add file list if available
                if result.output.files_created:
                    output_text += f"\n\n**Files:** {', '.join(result.output.files_created[:10])}"
            else:
                output_text = str(result.output) if result.output else result.error or 'No output'

            # Build formatted result with usage info
            formatted.append(f"""
## Subtask {i} {status}
**Task:** {result.task}
**Status:** {'Completed' if result.success else 'Failed'}
**Duration:** {result.duration_seconds:.1f}s | **Credits:** {result.usage.credits_charged} | **Steps:** {result.steps_taken}

**Output:**
{output_text}
""")

        # Add summary footer
        summary = f"\n---\n**Total:** {len(results)} subtasks, {total_duration:.1f}s, {total_credits} credits"

        return "\n---\n".join(formatted) + summary

    def get_total_usage(self) -> SubAgentUsage:
        """Get total usage across all sub-agents in this session.

        Returns:
            SubAgentUsage with aggregated statistics
        """
        return self._total_usage

    def get_usage_summary(self) -> Dict[str, Any]:
        """Get usage summary for all sub-agents in this session.

        Returns:
            Dictionary with usage statistics
        """
        return {
            'total_sub_agents': len(self._results),
            'successful': sum(1 for r in self._results.values() if r.success),
            'failed': sum(1 for r in self._results.values() if not r.success),
            'total_credits': self._total_usage.credits_charged,
            'total_tokens': self._total_usage.total_tokens,
            'total_api_cost_usd': self._total_usage.api_cost_usd,
            'total_llm_calls': self._total_usage.llm_calls
        }
    
    async def cleanup(self):
        """Cleanup all sub-agents."""
        for agent_id, agent in self._sub_agents.items():
            try:
                if hasattr(agent, 'cleanup'):
                    await agent.cleanup()
                self.logger.debug(f"Cleaned up sub-agent {agent_id}")
            except Exception as e:
                self.logger.warning(f"Error cleaning up sub-agent {agent_id}: {e}")
        
        self._sub_agents.clear()
        self._results.clear()
        self._parent_map.clear()

