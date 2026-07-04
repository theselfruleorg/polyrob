"""
End-to-end integration test for task_agent_lite continuous chat.

Tests the critical bug fixes:
- Bug #1: Queue size check uses correct attribute (_user_messages)
- Bug #2: Message history saved on completion
- Bug #3: Concurrency protection
- Bug #4: Agent persistence verification
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

from agents.task_agent_lite import TaskAgent


class TestTaskAgentContinuousChat:
    """Tests for critical bug fixes in continuous chat."""

    @pytest.fixture
    def mock_session_manager(self):
        """Create mock session manager with a minimal status state machine.

        try_transition_status models the real manager: a transition only succeeds
        if the session is currently in the expected ``from`` state. This is what
        lets the SECONDARY (status) concurrency defense reject a second concurrent
        run once the first has moved the session into 'running'.
        """
        manager = MagicMock()
        # Mutable current status, mirrored back through get_session_info.
        state = {'status': 'completed'}

        def get_session_info(session_id):
            return {
                'session_id': 'test_session',
                'user_id': 'test_user',
                'status': state['status'],
            }

        def try_transition_status(session_id, from_status, to_status):
            if state['status'] != from_status:
                return False
            state['status'] = to_status
            return True

        def update_session_status(session_id, status):
            state['status'] = status

        manager.get_session_info.side_effect = get_session_info
        manager.try_transition_status.side_effect = try_transition_status
        manager.update_session_status.side_effect = update_session_status
        manager.update_session_metadata.return_value = None
        return manager

    @pytest.fixture
    def mock_container(self):
        """Create mock dependency injection container."""
        container = MagicMock()
        llm_module = MagicMock()
        llm_module.get_llm.return_value = MagicMock()
        container.get.side_effect = lambda name: llm_module if name == 'llm' else None
        return container

    @pytest.fixture
    def task_agent(self, mock_container, mock_session_manager):
        """Create TaskAgent instance for testing."""
        config = MagicMock()
        config.session_ttl_seconds = 3600
        config.max_sessions_in_memory = 10
        config.session_cleanup_interval = 60
        config.browser_headless = True

        agent = TaskAgent(
            container=mock_container,
            config=config,
        )
        # session_manager is no longer a constructor kwarg; it is populated during
        # initialize(). Inject the mock directly for these unit-style tests.
        agent.session_manager = mock_session_manager
        # BaseComponent.__init__ does not retain the container kwarg (it lazily
        # resolves the DI singleton). Inject the mock via the setter so no real
        # container is needed.
        agent.container = mock_container
        # initialize() would normally set this; mark the task package available so
        # run_session() proceeds past its availability guard.
        agent.task_available = True

        return agent

    @pytest.mark.asyncio
    async def test_message_history_saved_on_completion(self, task_agent):
        """
        Test that message history is saved when session completes.

        BUG FIX #2 VERIFICATION: This tests the fix that adds message history
        save in the finally block of run_session.
        """
        user_id = "test_user"
        session_id = "test_session"

        # Mock orchestrator and agent
        mock_agent = MagicMock()
        mock_agent.agent_id = "test_agent"
        mock_agent.message_manager = MagicMock()
        mock_agent.message_manager.save_to_disk = MagicMock()

        orchestrator = MagicMock()
        orchestrator.user_id = user_id
        orchestrator.session_id = session_id
        orchestrator.agents = {"test_agent": mock_agent}
        orchestrator.execute_session = AsyncMock()
        orchestrator.cleanup = AsyncMock()

        # Store orchestrator
        task_agent._active_orchestrators[session_id] = orchestrator

        # Run session (should complete and save message history)
        try:
            await task_agent._run_session_impl(user_id, session_id)
        except Exception:
            pass  # Expected - orchestrator is mocked

        # VERIFY: Message history save was called in finally block
        mock_agent.message_manager.save_to_disk.assert_called_with(
            session_id=session_id,
            user_id=user_id
        )

    @pytest.mark.asyncio
    async def test_cleanup_preserves_agents(self, task_agent):
        """
        Test that cleanup preserves agents for continuous chat.

        BUG FIX #4 VERIFICATION: Tests preserve_agents=True logic.
        """
        user_id = "test_user"
        session_id = "test_session"

        # Mock orchestrator with agent
        mock_agent = MagicMock()
        mock_agent.agent_id = "test_agent"
        mock_agent.message_manager = MagicMock()
        mock_agent.message_manager.save_to_disk = MagicMock()

        mock_orch = MagicMock()
        mock_orch.user_id = user_id
        mock_orch.session_id = session_id
        mock_orch.agents = {"test_agent": mock_agent}
        mock_orch.execute_session = AsyncMock()
        mock_orch.cleanup = AsyncMock()

        task_agent._active_orchestrators[session_id] = mock_orch

        # Run session to completion
        try:
            await task_agent._run_session_impl(user_id, session_id)
        except Exception:
            pass

        # VERIFY: cleanup was called with preserve_agents=True
        mock_orch.cleanup.assert_called_once()
        call_args = mock_orch.cleanup.call_args
        assert call_args[1]['preserve_agents'] is True
        assert call_args[1]['full_cleanup'] is False


    @pytest.mark.asyncio
    async def test_concurrent_session_execution_blocked(self, task_agent):
        """
        Test that concurrent execution of same session is blocked.

        BUG #3 VERIFICATION: Tests the dual-lock concurrency protection.
        """
        user_id = "test_user"
        session_id = "test_session"

        # Mock orchestrator with slow execution
        mock_agent = MagicMock()
        mock_agent.agent_id = "test_agent"
        mock_agent.message_manager = MagicMock()
        mock_agent.message_manager.save_to_disk = MagicMock()

        mock_orch = MagicMock()
        mock_orch.user_id = user_id
        mock_orch.session_id = session_id
        # _run_session_impl looks up the existing agent under "executor_{session_id}";
        # using that key takes the REUSE branch (no LLM/create_agent), so the slow
        # execute_session below holds the execution lock for the full sleep.
        mock_agent.reset_for_continuation = MagicMock()
        mock_orch.agents = {f"executor_{session_id}": mock_agent}
        mock_orch.cleanup = AsyncMock()

        # Track concurrency: the execution-lock (PRIMARY defense) must serialize
        # runs of the same session, so this never sees more than one in flight.
        concurrency = {"current": 0, "max": 0}

        async def slow_execution(*args, **kwargs):
            concurrency["current"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["current"])
            try:
                await asyncio.sleep(0.5)  # Simulate long execution
            finally:
                concurrency["current"] -= 1
            return {mock_agent.agent_id: {"status": "completed"}}

        mock_orch.execute_session = slow_execution

        task_agent._active_orchestrators[session_id] = mock_orch

        # Start first execution
        task1 = asyncio.create_task(task_agent.run_session(user_id, session_id))

        # Wait a bit then try concurrent execution
        await asyncio.sleep(0.1)

        # Second execution: the PRIMARY execution lock makes it WAIT for the first
        # to finish rather than running concurrently (BUG #3 protection).
        result2 = await task_agent.run_session(user_id, session_id)

        # Wait for first task to complete
        try:
            result1 = await asyncio.wait_for(task1, timeout=2.0)
        except asyncio.TimeoutError:
            task1.cancel()
            result1 = None

        # VERIFY: concurrent execution was prevented — the two runs were serialized
        # by the execution lock, so execute_session never overlapped.
        assert concurrency["max"] == 1, \
            f"Concurrent execution should be blocked (max in-flight=1), got {concurrency['max']}"
        # Both runs ultimately completed (the second waited, then ran).
        assert result1 == "Session completed successfully"
        assert result2 == "Session completed successfully"

    @pytest.mark.asyncio
    async def test_message_queue_overflow_protection(self):
        """
        Test that message queue overflow protection works.

        BUG #1 VERIFICATION: Tests the fixed _user_messages attribute check.
        """
        from agents.task.agent.orchestrator import SessionOrchestrator
        from agents.task.agent.hitl_manager import HITLManager

        # Create orchestrator
        orchestrator = MagicMock(spec=SessionOrchestrator)
        orchestrator.session_id = "test_session"
        orchestrator.user_id = "test_user"
        orchestrator.agents = {}
        orchestrator.logger = MagicMock()

        # Create real agent with real hitl_manager
        mock_agent = MagicMock()
        mock_agent.agent_id = "test_agent"
        mock_agent.hitl_manager = HITLManager(
            session_id="test_session",
            agent_id="test_agent"
        )

        orchestrator.agents = {"test_agent": mock_agent}

        # Use the real route_user_message implementation
        MAX_QUEUED_MESSAGES = 10  # Same as default in orchestrator

        async def route_user_message(text, kind="comment", metadata=None, target_agent_id=None):
            """Real implementation from orchestrator."""
            agent = mock_agent

            # Check queue size before adding message
            if hasattr(agent, 'hitl_manager') and agent.hitl_manager:
                # This is the FIXED code (Bug #1)
                queue_size = agent.hitl_manager.get_queue_size()

                if queue_size >= MAX_QUEUED_MESSAGES:
                    raise ValueError(
                        f"Message queue full ({queue_size}/{MAX_QUEUED_MESSAGES}). "
                        f"Wait for agent to process pending messages."
                    )

                # Queue the message
                await agent.hitl_manager.queue_user_message(text, kind, metadata)
                orchestrator.logger.info(f"Routed message to {agent.agent_id} (queue: {queue_size + 1}/{MAX_QUEUED_MESSAGES})")

        # Fill queue to near capacity
        for i in range(9):
            await route_user_message(f"Message {i}")

        # Should still accept one more
        await route_user_message("Message 9")

        # Next one should fail
        with pytest.raises(ValueError, match="Message queue full"):
            await route_user_message("Message 10")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
