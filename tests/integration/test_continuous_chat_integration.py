"""
Integration test for continuous chat mode.

Tests the full agent with continuous chat features enabled.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# Native message types
from modules.llm.messages import AIMessage, HumanMessage

from agents.task.agent.service import Agent
from agents.task.agent.orchestrator import SessionOrchestrator


class TestContinuousChatIntegration:
    """Integration tests for continuous chat with real agent."""

    @pytest.mark.asyncio
    async def test_agent_supports_streaming_check(self):
        """Test that agent correctly identifies streaming support."""
        # Create minimal orchestrator
        orchestrator = MagicMock()
        orchestrator.session_id = "test_session"
        orchestrator.user_id = "test_user"
        orchestrator.container = None

        # Mock message manager
        message_manager = MagicMock()
        message_manager.provider_name = "openai"

        # Create agent with minimal config
        agent = MagicMock()
        agent.message_manager = message_manager
        agent.provider_name = "openai"

        # Test _supports_streaming
        from agents.task.agent.service import Agent
        result = Agent._supports_streaming(agent)

        assert result is True

    @pytest.mark.asyncio
    async def test_message_injection_during_step(self):
        """Test that messages queued during a step are picked up."""
        from agents.task.agent.hitl_manager import HITLManager

        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Simulate step 1 execution
        # User sends message during step
        await hitl_manager.queue_user_message("Change the approach")

        # Agent checks messages after step (as implemented)
        messages = await hitl_manager.drain_user_messages()

        assert len(messages) == 1
        assert messages[0]['text'] == "Change the approach"

    @pytest.mark.asyncio
    async def test_streaming_with_mock_llm(self):
        """Test streaming output with mocked LLM."""
        from agents.task.agent.hitl_manager import HITLManager

        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Register callback to capture stream
        captured = []
        async def capture(chunk):
            captured.append(chunk)

        hitl_manager.register_output_callback(capture)

        # Simulate LLM streaming
        chunks = ["Hello", " ", "world", "!"]
        for chunk in chunks:
            await hitl_manager.stream_output(chunk)

        assert captured == chunks
        assert ''.join(captured) == "Hello world!"

    @pytest.mark.asyncio
    async def test_wait_for_user_then_continue(self):
        """Test agent can wait for user input and continue."""
        from agents.task.agent.hitl_manager import HITLManager

        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Simulate agent asking user a question
        async def user_responds_later():
            await asyncio.sleep(0.3)
            await hitl_manager.queue_user_message("prod")

        task = asyncio.create_task(user_responds_later())

        # Agent waits for response
        response = await hitl_manager.wait_for_user_input(
            "Which database: prod or dev?",
            timeout=2
        )

        await task  # Wait for background task

        assert response == "prod"

    @pytest.mark.asyncio
    async def test_continue_after_done_pattern(self):
        """Test the pattern of continuing after agent marks done."""
        from agents.task.agent.hitl_manager import HITLManager
        from agents.task.agent.views import ActionResult

        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Simulate agent finishing task
        result = ActionResult(
            is_done=True,
            extracted_content="Task completed",
            include_in_memory=True
        )

        # User sends follow-up immediately
        await hitl_manager.queue_user_message("Now also do X")

        # Check if user sent more messages
        await asyncio.sleep(0.1)  # Small delay as in implementation
        messages = await hitl_manager.drain_user_messages()

        if messages:
            # Reset done flag (as in implementation)
            result.is_done = False

        # Agent should continue
        assert result.is_done is False
        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_multiple_messages_during_execution(self):
        """Test multiple messages arriving during execution."""
        from agents.task.agent.hitl_manager import HITLManager

        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # User sends multiple messages
        await hitl_manager.queue_user_message("msg1")
        await hitl_manager.queue_user_message("msg2")
        await hitl_manager.queue_user_message("msg3")
        await hitl_manager.queue_user_message("msg4")
        await hitl_manager.queue_user_message("msg5")

        # First drain (max 3)
        batch1 = await hitl_manager.drain_user_messages()
        assert len(batch1) == 3

        # Second drain (remaining 2)
        batch2 = await hitl_manager.drain_user_messages()
        assert len(batch2) == 2

        # All messages processed
        batch3 = await hitl_manager.drain_user_messages()
        assert len(batch3) == 0

    @pytest.mark.asyncio
    async def test_streaming_error_recovery(self):
        """Test that streaming continues even if one callback fails."""
        from agents.task.agent.hitl_manager import HITLManager

        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Register failing callback
        async def failing_callback(chunk):
            raise RuntimeError("Callback error")

        # Register working callback
        captured = []
        async def working_callback(chunk):
            captured.append(chunk)

        hitl_manager.register_output_callback(failing_callback)
        hitl_manager.register_output_callback(working_callback)

        # Stream should continue despite failing callback
        await hitl_manager.stream_output("chunk1")
        await hitl_manager.stream_output("chunk2")

        # Working callback should have received chunks
        assert captured == ["chunk1", "chunk2"]

    @pytest.mark.asyncio
    async def test_concurrent_message_queueing(self):
        """Test concurrent message queueing is thread-safe."""
        from agents.task.agent.hitl_manager import HITLManager

        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Queue messages concurrently
        async def queue_messages(prefix, count):
            for i in range(count):
                await hitl_manager.queue_user_message(f"{prefix}_{i}")

        # Run multiple queueing tasks concurrently
        await asyncio.gather(
            queue_messages("A", 3),
            queue_messages("B", 3),
            queue_messages("C", 3)
        )

        # All messages should be queued
        all_messages = []
        while True:
            batch = await hitl_manager.drain_user_messages()
            if not batch:
                break
            all_messages.extend(batch)

        assert len(all_messages) == 9

    @pytest.mark.asyncio
    async def test_wait_for_input_timeout_handling(self):
        """Test timeout handling in wait_for_user_input."""
        from agents.task.agent.hitl_manager import HITLManager

        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Wait with short timeout, no response
        response = await hitl_manager.wait_for_user_input(
            "Question?",
            timeout=1
        )

        # Should timeout and return None
        assert response is None

    @pytest.mark.asyncio
    async def test_message_metadata_preserved(self):
        """Test that message metadata is preserved through queue."""
        from agents.task.agent.hitl_manager import HITLManager

        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Queue message with metadata
        await hitl_manager.queue_user_message(
            "test message",
            kind="comment",
            metadata={"source": "api", "priority": "high"}
        )

        # Drain and check metadata
        messages = await hitl_manager.drain_user_messages()

        assert len(messages) == 1
        assert messages[0]['text'] == "test message"
        assert messages[0]['kind'] == "comment"
        assert messages[0]['metadata']['source'] == "api"
        assert messages[0]['metadata']['priority'] == "high"
        assert 'timestamp' in messages[0]


class TestContinuousChatEndToEnd:
    """End-to-end scenarios for continuous chat."""

    @pytest.mark.asyncio
    async def test_full_chat_scenario(self):
        """Test a full continuous chat scenario."""
        from agents.task.agent.hitl_manager import HITLManager

        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Setup streaming
        output = []
        async def capture_output(chunk):
            output.append(chunk)

        hitl_manager.register_output_callback(capture_output)

        # Scenario:
        # 1. Agent starts task
        # 2. User sends guidance during execution
        await hitl_manager.queue_user_message("Use dev database")

        # 3. Agent processes message
        messages = await hitl_manager.drain_user_messages()
        assert len(messages) == 1

        # 4. Agent streams output
        await hitl_manager.stream_output("Processing with dev database...")
        assert len(output) == 1

        # 5. Agent asks question
        async def user_answers():
            await asyncio.sleep(0.2)
            await hitl_manager.queue_user_message("yes, continue")

        asyncio.create_task(user_answers())
        answer = await hitl_manager.wait_for_user_input("Continue?", timeout=2)
        assert answer == "yes, continue"

        # 6. Agent completes and streams final output
        await hitl_manager.stream_output("Done!")
        assert len(output) == 2

        # 7. User sends follow-up
        await hitl_manager.queue_user_message("Now do the production migration")

        # 8. Agent picks up follow-up and continues
        followup = await hitl_manager.drain_user_messages()
        assert len(followup) == 1
        assert followup[0]['text'] == "Now do the production migration"
