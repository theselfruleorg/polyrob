"""
Unit tests for BrowserManager component.

Tests browser lifecycle management, context pooling, per-session limits,
wait queues, and cleanup behavior.
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call
from typing import Optional

from core.config import BotConfig
from tools.browser.browser_manager import BrowserManager, BrowserManagerConfig
from tools.browser.context import BrowserContext, BrowserContextConfig


# All async test methods in this module run under pytest-asyncio (strict mode).
pytestmark = pytest.mark.asyncio


# NOTE: these fixtures build plain objects (no `await`), so they are sync
# fixtures. Under pytest-asyncio strict mode a plain `@pytest.fixture` on an
# `async def` would yield an un-awaited coroutine instead of the value.
@pytest.fixture
def mock_browser():
    """Create a mock Browser instance."""
    browser = MagicMock()
    browser.new_context = AsyncMock()
    browser.close = AsyncMock()
    return browser


@pytest.fixture
def mock_browser_context():
    """Create a mock BrowserContext instance."""
    context = MagicMock(spec=BrowserContext)
    context.reset_context = AsyncMock()
    context.close = AsyncMock()
    context.config = BrowserContextConfig()
    context.allowed_domains = []
    return context


@pytest.fixture
def browser_manager_config():
    """Create a test configuration for BrowserManager."""
    return BrowserManagerConfig(
        max_contexts=2,
        max_contexts_per_session=1,
        context_timeout=5.0,
        wait_queue_timeout=2.0,
        enable_pooling=True,
        stale_context_timeout=60.0
    )


@pytest.fixture
def bot_config(browser_manager_config):
    """Create a test BotConfig."""
    config = BotConfig()
    config.max_browser_contexts = browser_manager_config.max_contexts
    config.max_contexts_per_session = browser_manager_config.max_contexts_per_session
    config.browser_context_timeout = browser_manager_config.context_timeout
    config.browser_stale_timeout = browser_manager_config.stale_context_timeout
    config.browser_enable_pooling = browser_manager_config.enable_pooling
    return config


class TestBrowserManagerInitialization:
    """Test BrowserManager initialization."""

    async def test_initialization_with_config(self, bot_config):
        """Test that BrowserManager initializes correctly with config."""
        manager = BrowserManager(config=bot_config)

        assert manager.config == bot_config
        assert manager.browser is None
        assert manager.context_pool == []
        assert manager.contexts_in_use == {}
        assert manager.session_contexts == {}
        assert manager.browser_config.max_contexts == 2
        assert manager.browser_config.max_contexts_per_session == 1

    async def test_initialization_without_config(self):
        """Test that BrowserManager initializes with default config."""
        manager = BrowserManager()

        assert manager.config is not None
        assert isinstance(manager.config, BotConfig)
        # Default value comes from BotConfig.max_browser_contexts (Field default).
        assert manager.browser_config.max_contexts == BotConfig().max_browser_contexts

    @patch('tools.browser.browser_manager.Browser')
    async def test_browser_initialization(self, mock_browser_class, bot_config):
        """Test that browser is initialized correctly."""
        mock_browser_instance = MagicMock()
        mock_browser_class.return_value = mock_browser_instance

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        mock_browser_class.assert_called_once()
        assert manager.browser == mock_browser_instance

    @patch('tools.browser.browser_manager.Browser')
    async def test_background_tasks_not_started_on_initialize(self, mock_browser_class, bot_config):
        """Regression guard: BrowserManager.initialize() must not spawn the
        stale-context/wait-queue loops until a context is actually requested --
        otherwise every server process pays a 1s poll forever even when the
        browser tool is never used."""
        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        assert manager._cleanup_task is None
        assert manager._queue_processor_task is None

    def test_wait_queue_backstop_interval_default(self, bot_config):
        manager = BrowserManager(config=bot_config)
        assert manager.browser_config.wait_queue_backstop_interval == 15.0

    def test_wait_queue_backstop_interval_configurable(self):
        config = BotConfig()
        config.browser_wait_queue_backstop_interval = 5.0
        manager = BrowserManager(config=config)
        assert manager.browser_config.wait_queue_backstop_interval == 5.0


class TestContextAllocation:
    """Test context allocation and pooling."""

    @patch('tools.browser.browser_manager.Browser')
    async def test_get_context_creates_new(self, mock_browser_class, bot_config, mock_browser_context):
        """Test that get_context creates a new context when none exist."""
        mock_browser = mock_browser_class.return_value
        mock_browser.new_context = AsyncMock(return_value=mock_browser_context)

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        context = await manager.get_context("session1")

        assert context == mock_browser_context
        assert "session1" in manager.contexts_in_use
        assert manager.contexts_in_use["session1"] == mock_browser_context
        mock_browser.new_context.assert_called_once()

    @patch('tools.browser.browser_manager.Browser')
    async def test_get_context_returns_existing(self, mock_browser_class, bot_config, mock_browser_context):
        """Test that get_context returns existing context for same session."""
        mock_browser = mock_browser_class.return_value
        mock_browser.new_context = AsyncMock(return_value=mock_browser_context)

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        context1 = await manager.get_context("session1")
        context2 = await manager.get_context("session1")

        assert context1 == context2
        assert mock_browser.new_context.call_count == 1

    @patch('tools.browser.browser_manager.Browser')
    async def test_background_tasks_started_lazily_on_first_get_context(
        self, mock_browser_class, bot_config, mock_browser_context
    ):
        mock_browser_instance = MagicMock()
        mock_browser_instance.new_context = AsyncMock(return_value=mock_browser_context)
        mock_browser_class.return_value = mock_browser_instance

        manager = BrowserManager(config=bot_config)
        await manager.initialize()
        assert manager._queue_processor_task is None

        await manager.get_context("session-1")

        assert manager._queue_processor_task is not None
        assert not manager._queue_processor_task.done()
        # cleanup so the test doesn't leak a running task
        await manager.cleanup()

    @patch('tools.browser.browser_manager.Browser')
    async def test_per_session_limit(self, mock_browser_class, bot_config, mock_browser_context):
        """Test that per-session context limit is enforced."""
        mock_browser = mock_browser_class.return_value
        mock_browser.new_context = AsyncMock(return_value=mock_browser_context)

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        # Create context for session1_agent1
        context1 = await manager.get_context("session1_agent1")
        assert context1 is not None

        # Try to create another context for same base session (should fail)
        context2 = await manager.get_context("session1_agent2")
        assert context2 is None  # Per-session limit reached

    @patch('tools.browser.browser_manager.Browser')
    async def test_global_limit(self, mock_browser_class, bot_config):
        """Test that global context limit is enforced."""
        mock_browser = mock_browser_class.return_value

        async def create_mock_context(config=None, session_id=None):
            ctx = MagicMock()
            ctx.reset_context = AsyncMock()
            ctx.close = AsyncMock()
            return ctx

        mock_browser.new_context = create_mock_context

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        # Create max contexts
        context1 = await manager.get_context("session1")
        context2 = await manager.get_context("session2")

        assert context1 is not None
        assert context2 is not None

        # Try to create one more (should fail due to global limit)
        context3 = await manager.get_context("session3")
        assert context3 is None


class TestContextPooling:
    """Test context pooling and reuse."""

    @patch('tools.browser.browser_manager.Browser')
    async def test_context_returned_to_pool(self, mock_browser_class, bot_config, mock_browser_context):
        """Test that released contexts are returned to pool."""
        mock_browser = mock_browser_class.return_value
        mock_browser.new_context = AsyncMock(return_value=mock_browser_context)

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        context = await manager.get_context("session1")
        assert len(manager.context_pool) == 0

        await manager.release_context("session1", close=False)
        assert len(manager.context_pool) == 1
        assert "session1" not in manager.contexts_in_use

    @patch('tools.browser.browser_manager.Browser')
    async def test_context_reused_from_pool(self, mock_browser_class, bot_config, mock_browser_context):
        """Test that contexts are reused from pool."""
        mock_browser = mock_browser_class.return_value
        mock_browser.new_context = AsyncMock(return_value=mock_browser_context)

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        # Create and release a context
        context1 = await manager.get_context("session1")
        await manager.release_context("session1", close=False)

        # Get context for different session (should reuse from pool)
        context2 = await manager.get_context("session2", reuse=True)

        assert context2 == context1
        assert mock_browser.new_context.call_count == 1
        mock_browser_context.reset_context.assert_called_once()

    @patch('tools.browser.browser_manager.Browser')
    async def test_context_closed_when_close_true(self, mock_browser_class, bot_config, mock_browser_context):
        """Test that contexts are closed when close=True."""
        mock_browser = mock_browser_class.return_value
        mock_browser.new_context = AsyncMock(return_value=mock_browser_context)

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        context = await manager.get_context("session1")
        await manager.release_context("session1", close=True)

        assert len(manager.context_pool) == 0
        mock_browser_context.close.assert_called_once()


class TestWaitQueue:
    """Test wait queue functionality."""

    @patch('tools.browser.browser_manager.Browser')
    async def test_wait_queue_basic(self, mock_browser_class, bot_config):
        """Test that sessions can wait in queue for contexts."""
        mock_browser = mock_browser_class.return_value

        async def create_mock_context(config=None, session_id=None):
            ctx = MagicMock()
            ctx.reset_context = AsyncMock()
            ctx.close = AsyncMock()
            return ctx

        mock_browser.new_context = create_mock_context

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        # Fill up all contexts
        context1 = await manager.get_context("session1")
        context2 = await manager.get_context("session2")

        # Start a waiter
        waiter_task = asyncio.create_task(
            manager.get_context("session3", wait=True, timeout=5.0)
        )

        # Give it time to enter the queue
        await asyncio.sleep(0.1)
        assert len(manager._wait_queue) == 1

        # Release a context
        await manager.release_context("session1", close=True)

        # Waiter should get a context
        context3 = await asyncio.wait_for(waiter_task, timeout=1.0)
        assert context3 is not None

    @patch('tools.browser.browser_manager.Browser')
    async def test_wait_queue_timeout(self, mock_browser_class, bot_config):
        """Test that wait queue times out properly."""
        mock_browser = mock_browser_class.return_value

        async def create_mock_context(config=None, session_id=None):
            ctx = MagicMock()
            ctx.reset_context = AsyncMock()
            ctx.close = AsyncMock()
            return ctx

        mock_browser.new_context = create_mock_context

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        # Fill up all contexts
        await manager.get_context("session1")
        await manager.get_context("session2")

        # Try to get context with short timeout
        context = await manager.get_context("session3", wait=True, timeout=0.1)

        assert context is None
        assert len(manager._wait_queue) == 0


class TestCleanup:
    """Test cleanup and resource management."""

    @patch('tools.browser.browser_manager.Browser')
    async def test_cleanup_releases_all_contexts(self, mock_browser_class, bot_config):
        """Test that cleanup releases all contexts."""
        mock_browser = mock_browser_class.return_value

        async def create_mock_context(config=None, session_id=None):
            ctx = MagicMock()
            ctx.reset_context = AsyncMock()
            ctx.close = AsyncMock()
            return ctx

        mock_browser.new_context = create_mock_context

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        # Create some contexts
        context1 = await manager.get_context("session1")
        context2 = await manager.get_context("session2")

        # Cleanup
        await manager.cleanup()

        assert len(manager.contexts_in_use) == 0
        assert len(manager.context_pool) == 0
        mock_browser.close.assert_called_once()

    @patch('tools.browser.browser_manager.Browser')
    async def test_release_context_idempotent(self, mock_browser_class, bot_config, mock_browser_context):
        """Test that release_context is idempotent."""
        mock_browser = mock_browser_class.return_value
        mock_browser.new_context = AsyncMock(return_value=mock_browser_context)

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        context = await manager.get_context("session1")

        # Release twice - should not error
        await manager.release_context("session1")
        await manager.release_context("session1")

        assert "session1" not in manager.contexts_in_use


class TestSessionTracking:
    """Test per-session tracking and configuration."""

    @patch('tools.browser.browser_manager.Browser')
    async def test_session_config_stored(self, mock_browser_class, bot_config, mock_browser_context):
        """Test that session configurations are stored."""
        mock_browser = mock_browser_class.return_value
        mock_browser.new_context = AsyncMock(return_value=mock_browser_context)

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        config = BrowserContextConfig(allowed_domains=["example.com"])
        context = await manager.get_context("session1", config=config)

        assert "session1" in manager.session_configs
        assert manager.session_configs["session1"] == config

    @patch('tools.browser.browser_manager.Browser')
    async def test_allowed_domains_enforced(self, mock_browser_class, bot_config, mock_browser_context):
        """Test that allowed domains are set on contexts."""
        mock_browser = mock_browser_class.return_value
        mock_browser.new_context = AsyncMock(return_value=mock_browser_context)

        manager = BrowserManager(config=bot_config)
        await manager.initialize()

        allowed_domains = ["example.com", "test.com"]
        context = await manager.get_context("session1", allowed_domains=allowed_domains)

        # When reusing from pool, domains should be updated
        await manager.release_context("session1")
        context2 = await manager.get_context("session2", allowed_domains=["other.com"])

        # Reset should have been called
        mock_browser_context.reset_context.assert_called()

    @patch('tools.browser.browser_manager.Browser')
    async def test_forget_session_config_removes_cached_config(self, mock_browser_class, bot_config, mock_browser_context):
        mock_browser_instance = MagicMock()
        mock_browser_instance.new_context = AsyncMock(return_value=mock_browser_context)
        mock_browser_class.return_value = mock_browser_instance

        manager = BrowserManager(config=bot_config)
        await manager.initialize()
        await manager.get_context("session-1", allowed_domains=["example.com"])

        assert "session-1" in manager.session_configs

        manager.forget_session_config("session-1")

        assert "session-1" not in manager.session_configs
        await manager.cleanup()

    @patch('tools.browser.browser_manager.Browser')
    async def test_forget_session_config_is_noop_for_unknown_session(self, mock_browser_class, bot_config):
        manager = BrowserManager(config=bot_config)
        await manager.initialize()
        manager.forget_session_config("never-existed")  # must not raise
        await manager.cleanup()


@pytest.mark.asyncio
async def test_browser_manager_integration():
    """Integration test for BrowserManager with real components."""
    config = BotConfig()
    config.max_browser_contexts = 2
    config.browser_enable_pooling = True

    manager = BrowserManager(config=config)

    try:
        # Initialize should not fail even without real browser
        await manager.initialize()

        # Get status
        status = manager.get_status()
        assert 'contexts_in_use' in status
        assert 'contexts_pooled' in status
        assert 'max_contexts' in status

    finally:
        await manager.cleanup()