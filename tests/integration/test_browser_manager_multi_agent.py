"""
Integration tests for BrowserManager with multi-agent and multi-session scenarios.

Tests concurrent access patterns, per-session limits, wait queues, and proper cleanup.
"""

import asyncio
import pytest
import time
from typing import List, Optional
from unittest.mock import MagicMock, AsyncMock, patch

from core.config import BotConfig
from tools.browser.browser_manager import BrowserManager, BrowserManagerConfig
from agents.task.agent.orchestrator import SessionOrchestrator


class TestMultiAgentBrowserManagement:
    """Test multi-agent browser management scenarios."""

    @pytest.mark.asyncio
    async def test_per_session_limit_enforcement(self):
        """Test that per-session context limits are enforced."""
        config = BotConfig()
        manager_config = BrowserManagerConfig(
            max_contexts=6,
            max_contexts_per_session=2,
            enable_pooling=True
        )

        with patch('tools.browser.browser_manager.Browser') as MockBrowser:
            mock_browser = MockBrowser.return_value
            mock_browser.new_context = AsyncMock()
            mock_browser.close = AsyncMock()

            # Create mock contexts
            async def create_mock_context(config=None, session_id=None, **kwargs):
                ctx = MagicMock()
                ctx.reset_context = AsyncMock()
                ctx.close = AsyncMock()
                return ctx

            mock_browser.new_context.side_effect = create_mock_context

            manager = BrowserManager(config=config)
            manager.browser_config = manager_config
            await manager.initialize()

            # Session 1: Should get 2 contexts
            ctx1_1 = await manager.get_context("session1_agent1")
            ctx1_2 = await manager.get_context("session1_agent2")
            assert ctx1_1 is not None
            assert ctx1_2 is not None

            # Session 1: Third agent should fail (per-session limit)
            ctx1_3 = await manager.get_context("session1_agent3", wait=False)
            assert ctx1_3 is None

            # Session 2: Should get 2 contexts (different session)
            ctx2_1 = await manager.get_context("session2_agent1")
            ctx2_2 = await manager.get_context("session2_agent2")
            assert ctx2_1 is not None
            assert ctx2_2 is not None

            # Cleanup
            await manager.cleanup()

    @pytest.mark.asyncio
    async def test_wait_queue_fairness(self):
        """Test that wait queue processes requests in FIFO order."""
        config = BotConfig()
        manager_config = BrowserManagerConfig(
            max_contexts=2,
            max_contexts_per_session=1,
            wait_queue_timeout=5.0,
            enable_pooling=True
        )

        with patch('tools.browser.browser_manager.Browser') as MockBrowser:
            mock_browser = MockBrowser.return_value
            mock_browser.new_context = AsyncMock()
            mock_browser.close = AsyncMock()

            async def create_mock_context(config=None, session_id=None, **kwargs):
                ctx = MagicMock()
                ctx.reset_context = AsyncMock()
                ctx.close = AsyncMock()
                await asyncio.sleep(0.1)  # Simulate creation time
                return ctx

            mock_browser.new_context.side_effect = create_mock_context

            manager = BrowserManager(config=config)
            manager.browser_config = manager_config
            await manager.initialize()

            # Fill up all contexts
            ctx1 = await manager.get_context("session1")
            ctx2 = await manager.get_context("session2")
            assert ctx1 is not None
            assert ctx2 is not None

            # Queue multiple requests
            wait_tasks = []
            wait_order = []

            async def wait_and_record(session_id):
                start_time = time.time()
                ctx = await manager.get_context(session_id, wait=True, timeout=5.0)
                wait_time = time.time() - start_time
                if ctx:
                    wait_order.append(session_id)
                return ctx, wait_time

            # Start 3 waiters
            for i in range(3):
                task = asyncio.create_task(wait_and_record(f"session{i+3}"))
                wait_tasks.append(task)
                await asyncio.sleep(0.05)  # Ensure ordering

            # Wait for queue to populate
            await asyncio.sleep(0.2)
            assert len(manager._wait_queue) == 3

            # Release contexts one by one
            await manager.release_context("session1", close=True)
            await asyncio.sleep(0.5)  # Let queue process

            await manager.release_context("session2", close=True)
            await asyncio.sleep(0.5)  # Let queue process

            # Wait for some tasks to complete
            done, pending = await asyncio.wait(wait_tasks, timeout=2.0, return_when=asyncio.FIRST_COMPLETED)

            # Verify FIFO order
            assert len(wait_order) >= 2
            assert wait_order[0] == "session3"  # First in queue
            assert wait_order[1] == "session4"  # Second in queue

            # Cancel remaining tasks
            for task in pending:
                task.cancel()

            await manager.cleanup()

    @pytest.mark.asyncio
    async def test_context_pool_reuse(self):
        """Test that contexts are properly pooled and reused."""
        config = BotConfig()
        manager_config = BrowserManagerConfig(
            max_contexts=3,
            enable_pooling=True
        )

        with patch('tools.browser.browser_manager.Browser') as MockBrowser:
            mock_browser = MockBrowser.return_value
            created_contexts = []

            async def track_context_creation(config=None, session_id=None, **kwargs):
                ctx = MagicMock()
                ctx.reset_context = AsyncMock()
                ctx.close = AsyncMock()
                ctx.id = len(created_contexts)
                created_contexts.append(ctx)
                return ctx

            mock_browser.new_context = AsyncMock(side_effect=track_context_creation)
            mock_browser.close = AsyncMock()

            manager = BrowserManager(config=config)
            manager.browser_config = manager_config
            await manager.initialize()

            # Create and release a context
            ctx1 = await manager.get_context("session1")
            assert ctx1 is not None
            original_id = ctx1.id

            await manager.release_context("session1", close=False)  # Pool it
            assert len(manager.context_pool) == 1

            # Get a new context - should reuse from pool
            ctx2 = await manager.get_context("session2")
            assert ctx2 is not None
            assert ctx2.id == original_id  # Same context reused

            # Verify reset was called
            ctx1.reset_context.assert_called_once()

            # Only one context should have been created
            assert len(created_contexts) == 1

            await manager.cleanup()

    @pytest.mark.asyncio
    async def test_cleanup_with_active_contexts(self):
        """Test that cleanup properly handles active contexts and wait queue."""
        config = BotConfig()
        manager_config = BrowserManagerConfig(
            max_contexts=2,
            enable_pooling=True
        )

        with patch('tools.browser.browser_manager.Browser') as MockBrowser:
            mock_browser = MockBrowser.return_value
            mock_browser.new_context = AsyncMock()
            mock_browser.close = AsyncMock()

            async def create_mock_context(config=None, session_id=None, **kwargs):
                ctx = MagicMock()
                ctx.reset_context = AsyncMock()
                ctx.close = AsyncMock()
                return ctx

            mock_browser.new_context.side_effect = create_mock_context

            manager = BrowserManager(config=config)
            manager.browser_config = manager_config
            await manager.initialize()

            # Create active contexts
            ctx1 = await manager.get_context("session1")
            ctx2 = await manager.get_context("session2")

            # Start a waiter that won't complete
            wait_task = asyncio.create_task(
                manager.get_context("session3", wait=True, timeout=10.0)
            )

            # Give it time to enter queue
            await asyncio.sleep(0.1)

            # Cleanup should handle everything gracefully
            await manager.cleanup()

            # Verify contexts were closed
            ctx1.close.assert_called()
            ctx2.close.assert_called()

            # Verify browser was closed
            mock_browser.close.assert_called()

            # Wait task should be cancelled
            assert wait_task.cancelled() or wait_task.done()

    @pytest.mark.asyncio
    async def test_orchestrator_integration(self):
        """Test BrowserManager integration with SessionOrchestrator."""
        config = BotConfig()

        with patch('tools.browser.browser_manager.Browser') as MockBrowser:
            mock_browser = MockBrowser.return_value
            mock_browser.new_context = AsyncMock()
            mock_browser.close = AsyncMock()

            async def create_mock_context(config=None, session_id=None, **kwargs):
                ctx = MagicMock()
                ctx.reset_context = AsyncMock()
                ctx.close = AsyncMock()
                ctx.get_state = AsyncMock(return_value=MagicMock())
                return ctx

            mock_browser.new_context.side_effect = create_mock_context

            # Create BrowserManager
            manager = BrowserManager(config=config)
            await manager.initialize()

            # Create orchestrator with the manager
            orchestrator = SessionOrchestrator(
                session_id="test_session",
                user_id=123,
                config=config
            )
            orchestrator.browser_manager = manager

            # Get contexts through orchestrator
            ctx1 = await orchestrator.get_browser_context("agent1")
            ctx2 = await orchestrator.get_browser_context("agent2")

            assert ctx1 is not None
            assert ctx2 is not None

            # Release through orchestrator (returns agent1's context to the pool)
            await orchestrator.release_browser_context("agent1")

            # Per-session cleanup releases THIS session's browser contexts but
            # deliberately preserves the shared BrowserManager.browser (that is
            # only torn down at app shutdown — see SessionCleanupMixin.cleanup).
            await orchestrator.cleanup()

            # The session's remaining context (agent2) was released/closed...
            ctx2.close.assert_called()
            # ...but the shared browser instance is preserved, not destroyed.
            assert manager.browser is not None

    @pytest.mark.asyncio
    async def test_concurrent_operations(self):
        """Test concurrent context acquisition and release operations."""
        config = BotConfig()
        manager_config = BrowserManagerConfig(
            max_contexts=5,
            max_contexts_per_session=2,
            enable_pooling=True
        )

        with patch('tools.browser.browser_manager.Browser') as MockBrowser:
            mock_browser = MockBrowser.return_value
            mock_browser.new_context = AsyncMock()
            mock_browser.close = AsyncMock()

            async def create_mock_context(config=None, session_id=None, **kwargs):
                ctx = MagicMock()
                ctx.reset_context = AsyncMock()
                ctx.close = AsyncMock()
                await asyncio.sleep(0.05)  # Simulate creation time
                return ctx

            mock_browser.new_context.side_effect = create_mock_context

            manager = BrowserManager(config=config)
            manager.browser_config = manager_config
            await manager.initialize()

            # Run concurrent operations
            async def session_lifecycle(session_id: str, num_agents: int):
                """Simulate a session with multiple agents."""
                contexts = []

                # Acquire contexts
                for i in range(num_agents):
                    ctx = await manager.get_context(f"{session_id}_agent{i}")
                    if ctx:
                        contexts.append((f"{session_id}_agent{i}", ctx))
                    await asyncio.sleep(0.01)

                # Use contexts
                await asyncio.sleep(0.1)

                # Release contexts
                for ctx_id, _ in contexts:
                    await manager.release_context(ctx_id)

                return len(contexts)

            # Run multiple sessions concurrently
            tasks = [
                session_lifecycle("session1", 2),
                session_lifecycle("session2", 2),
                session_lifecycle("session3", 1),
            ]

            results = await asyncio.gather(*tasks)

            # All sessions should have gotten their contexts
            assert results[0] == 2  # session1 got 2 contexts
            assert results[1] == 2  # session2 got 2 contexts
            assert results[2] == 1  # session3 got 1 context

            # Verify final state
            status = manager.get_status()
            assert status['contexts_in_use'] == 0  # All released
            assert status['contexts_pooled'] <= 5  # Within limits

            await manager.cleanup()


@pytest.mark.asyncio
async def test_idempotent_release():
    """Test that release_context is idempotent."""
    config = BotConfig()

    with patch('tools.browser.browser_manager.Browser') as MockBrowser:
        mock_browser = MockBrowser.return_value
        mock_browser.new_context = AsyncMock()
        mock_browser.close = AsyncMock()

        async def create_mock_context(config=None, session_id=None, **kwargs):
            ctx = MagicMock()
            ctx.reset_context = AsyncMock()
            ctx.close = AsyncMock()
            return ctx

        mock_browser.new_context.side_effect = create_mock_context

        manager = BrowserManager(config=config)
        await manager.initialize()

        # Get a context
        ctx = await manager.get_context("session1")
        assert ctx is not None

        # Release multiple times - should not error
        await manager.release_context("session1")
        await manager.release_context("session1")
        await manager.release_context("session1")

        # Status should show no contexts in use
        status = manager.get_status()
        assert status['contexts_in_use'] == 0

        await manager.cleanup()