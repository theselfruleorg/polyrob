"""
Tests for continuous chat mode features.

Tests the 4 enhancements:
1. Message checking during steps
2. Streaming output callbacks
3. Agent questions (wait_for_user_input)
4. Continue after done
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from collections import deque
from datetime import datetime

from agents.task.agent.hitl_manager import HITLManager
from agents.task.agent.service import Agent


class TestHITLManagerStreaming:
    """Test streaming output functionality."""

    @pytest.fixture
    def hitl_manager(self):
        """Create HITLManager instance."""
        return HITLManager(
            session_id="test_session",
            agent_id="test_agent"
        )

    @pytest.mark.asyncio
    async def test_register_output_callback(self, hitl_manager):
        """Test registering output callback."""
        callback = AsyncMock()

        hitl_manager.register_output_callback(callback)

        assert callback in hitl_manager._output_callbacks
        assert len(hitl_manager._output_callbacks) == 1

    @pytest.mark.asyncio
    async def test_register_duplicate_callback(self, hitl_manager):
        """Test that duplicate callbacks are not added."""
        callback = AsyncMock()

        hitl_manager.register_output_callback(callback)
        hitl_manager.register_output_callback(callback)

        assert len(hitl_manager._output_callbacks) == 1

    @pytest.mark.asyncio
    async def test_stream_output_no_callbacks(self, hitl_manager):
        """Test streaming with no callbacks registered."""
        # Should not raise error
        await hitl_manager.stream_output("test chunk")

    @pytest.mark.asyncio
    async def test_stream_output_with_callback(self, hitl_manager):
        """Test streaming output to callback."""
        chunks = []

        async def capture_chunk(chunk):
            chunks.append(chunk)

        hitl_manager.register_output_callback(capture_chunk)

        await hitl_manager.stream_output("chunk 1")
        await hitl_manager.stream_output("chunk 2")
        await hitl_manager.stream_output("chunk 3")

        assert chunks == ["chunk 1", "chunk 2", "chunk 3"]

    @pytest.mark.asyncio
    async def test_stream_output_multiple_callbacks(self, hitl_manager):
        """Test streaming to multiple callbacks."""
        chunks1 = []
        chunks2 = []

        async def capture1(chunk):
            chunks1.append(chunk)

        async def capture2(chunk):
            chunks2.append(chunk)

        hitl_manager.register_output_callback(capture1)
        hitl_manager.register_output_callback(capture2)

        await hitl_manager.stream_output("test")

        assert chunks1 == ["test"]
        assert chunks2 == ["test"]

    @pytest.mark.asyncio
    async def test_stream_output_callback_error(self, hitl_manager):
        """Test that callback errors don't break streaming."""
        chunks = []

        async def failing_callback(chunk):
            raise ValueError("Test error")

        async def working_callback(chunk):
            chunks.append(chunk)

        hitl_manager.register_output_callback(failing_callback)
        hitl_manager.register_output_callback(working_callback)

        # Should not raise, working callback should still work
        await hitl_manager.stream_output("test")

        assert chunks == ["test"]


class TestHITLManagerWaitForInput:
    """Test wait_for_user_input functionality."""

    @pytest.fixture
    def hitl_manager(self):
        """Create HITLManager instance."""
        return HITLManager(
            session_id="test_session",
            agent_id="test_agent"
        )

    @pytest.mark.asyncio
    async def test_wait_for_user_input_immediate_response(self, hitl_manager):
        """Test immediate user response."""
        # Queue a message
        await hitl_manager.queue_user_message("dev database")

        # Wait for input
        response = await hitl_manager.wait_for_user_input(
            "Which database?",
            timeout=5
        )

        assert response == "dev database"

    @pytest.mark.asyncio
    async def test_wait_for_user_input_delayed_response(self, hitl_manager):
        """Test delayed user response."""
        async def add_message_later():
            await asyncio.sleep(0.5)
            await hitl_manager.queue_user_message("production")

        # Start adding message in background
        asyncio.create_task(add_message_later())

        # Wait for input
        response = await hitl_manager.wait_for_user_input(
            "Which database?",
            timeout=2
        )

        assert response == "production"

    @pytest.mark.asyncio
    async def test_wait_for_user_input_timeout(self, hitl_manager):
        """Test timeout when no response."""
        response = await hitl_manager.wait_for_user_input(
            "Which database?",
            timeout=1
        )

        assert response is None

    @pytest.mark.asyncio
    async def test_wait_for_user_input_multiple_messages(self, hitl_manager):
        """Test that only first message is returned."""
        await hitl_manager.queue_user_message("first")
        await hitl_manager.queue_user_message("second")

        response = await hitl_manager.wait_for_user_input(
            "Question?",
            timeout=1
        )

        # Should get first message
        assert response == "first"


class TestAgentSupportsStreaming:
    """Test _supports_streaming method."""

    @pytest.mark.asyncio
    async def test_supports_streaming_openai(self):
        """Test OpenAI provider supports streaming."""
        agent = MagicMock()
        agent.provider_name = "openai"

        from agents.task.agent.service import Agent
        result = Agent._supports_streaming(agent)

        assert result is True

    @pytest.mark.asyncio
    async def test_supports_streaming_anthropic(self):
        """Test Anthropic provider supports streaming."""
        agent = MagicMock()
        agent.provider_name = "Anthropic"

        from agents.task.agent.service import Agent
        result = Agent._supports_streaming(agent)

        assert result is True

    @pytest.mark.asyncio
    async def test_supports_streaming_google(self):
        """Test Google provider supports streaming."""
        agent = MagicMock()
        agent.provider_name = "Google"

        from agents.task.agent.service import Agent
        result = Agent._supports_streaming(agent)

        assert result is True

    @pytest.mark.asyncio
    async def test_supports_streaming_unsupported(self):
        """Test unsupported provider."""
        agent = MagicMock()
        agent.provider_name = "some_unknown_provider"

        from agents.task.agent.service import Agent
        result = Agent._supports_streaming(agent)

        assert result is False


class TestMessageQueueing:
    """Test message queueing and draining."""

    @pytest.fixture
    def hitl_manager(self):
        """Create HITLManager instance."""
        return HITLManager(
            session_id="test_session",
            agent_id="test_agent"
        )

    @pytest.mark.asyncio
    async def test_queue_and_drain_single_message(self, hitl_manager):
        """Test queueing and draining a single message."""
        await hitl_manager.queue_user_message("test message")

        messages = await hitl_manager.drain_user_messages()

        assert len(messages) == 1
        assert messages[0]['text'] == "test message"
        assert messages[0]['kind'] == "comment"

    @pytest.mark.asyncio
    async def test_queue_and_drain_multiple_messages(self, hitl_manager):
        """Test queueing and draining multiple messages."""
        await hitl_manager.queue_user_message("msg1")
        await hitl_manager.queue_user_message("msg2")
        await hitl_manager.queue_user_message("msg3")

        messages = await hitl_manager.drain_user_messages()

        # Should drain up to 3 messages
        assert len(messages) == 3
        assert messages[0]['text'] == "msg1"
        assert messages[1]['text'] == "msg2"
        assert messages[2]['text'] == "msg3"

    @pytest.mark.asyncio
    async def test_drain_respects_max_limit(self, hitl_manager):
        """Test that drain respects max_user_messages_per_step."""
        for i in range(5):
            await hitl_manager.queue_user_message(f"msg{i}")

        messages = await hitl_manager.drain_user_messages()

        # Should only drain 3 (default max)
        assert len(messages) == 3

        # Remaining messages should still be in queue
        assert len(hitl_manager._user_messages) == 2

    @pytest.mark.asyncio
    async def test_drain_empty_queue(self, hitl_manager):
        """Test draining empty queue."""
        messages = await hitl_manager.drain_user_messages()

        assert messages == []


class TestIntegrationContinuousChat:
    """Integration tests for continuous chat features."""

    @pytest.mark.asyncio
    async def test_streaming_end_to_end(self):
        """Test streaming output end-to-end."""
        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        captured_chunks = []

        async def capture(chunk):
            captured_chunks.append(chunk)

        hitl_manager.register_output_callback(capture)

        # Simulate streaming
        for i in range(5):
            await hitl_manager.stream_output(f"chunk{i}")

        assert len(captured_chunks) == 5
        assert captured_chunks == ["chunk0", "chunk1", "chunk2", "chunk3", "chunk4"]

    @pytest.mark.asyncio
    async def test_message_during_execution(self):
        """Test message arrival during execution."""
        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Simulate step execution
        await hitl_manager.queue_user_message("message during step")

        # Drain messages (as agent would do)
        messages = await hitl_manager.drain_user_messages()

        assert len(messages) == 1
        assert messages[0]['text'] == "message during step"

    @pytest.mark.asyncio
    async def test_wait_and_continue_pattern(self):
        """Test wait for input and continue pattern."""
        hitl_manager = HITLManager(
            session_id="test",
            agent_id="test"
        )

        # Simulate agent asking question
        async def user_responds():
            await asyncio.sleep(0.2)
            await hitl_manager.queue_user_message("yes, continue")

        asyncio.create_task(user_responds())

        answer = await hitl_manager.wait_for_user_input(
            "Should I continue?",
            timeout=2
        )

        assert answer == "yes, continue"

        # Agent can now continue based on answer
        assert answer is not None
