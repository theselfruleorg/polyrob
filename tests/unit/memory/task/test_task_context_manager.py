"""Unit tests for TaskContextManager."""

import pytest
import pytest_asyncio
import asyncio
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from modules.memory.task.task_context_manager import TaskContextManager


class FakeConfig:
    """Lightweight config stand-in.

    TaskContextManager only needs a truthy config object that exposes a
    dict-like ``.get(key, default)`` (the real BotConfig is a frozen pydantic
    model and no longer supports the old ``config.data = {...}`` assignment).

    Disables semantic retrieval so tests don't depend on RAG/embeddings, and
    sets DATA_PATH so the manager's base path is rooted under a temp dir
    (base path = DATA_PATH / "auto"). ``data`` is kept so existing path
    assertions can read the configured directory.
    """

    def __init__(self, values: dict):
        self.data = dict(values)

    def get(self, key, default=None):
        return self.data.get(key, default)


class TestTaskContextManager:
    """Test TaskContextManager functionality."""

    @pytest.fixture
    def config(self):
        """Create test config."""
        data_dir = tempfile.mkdtemp()
        # DATA_PATH drives the manager's base path (DATA_PATH / "auto"); keep
        # DATA_DIR identical so on-disk path assertions match what the manager
        # actually writes.
        return FakeConfig({
            "HIERARCHICAL_MEMORY_ENABLED": True,
            "MAX_RECENT_STEPS": 20,
            "SEMANTIC_RETRIEVAL_ENABLED": False,
            "DATA_DIR": data_dir,
            "DATA_PATH": data_dir,
        })

    @staticmethod
    def _make_manager(config):
        """Construct a TaskContextManager with dependency validation stubbed.

        BaseComponent.initialize() validates dependencies against the global
        DependencyContainer, which requires a fully configured container. The
        manager declares no required dependencies, so we inject a stub container
        (has_service -> True) to keep these unit tests isolated from the global
        container/DI bootstrap.
        """
        mgr = TaskContextManager(name="test_manager", config=config)
        stub_container = MagicMock()
        stub_container.has_service.return_value = True
        mgr.container = stub_container  # uses BaseComponent.container setter
        return mgr

    @pytest_asyncio.fixture
    async def manager(self, config):
        """Create task context manager."""
        mgr = self._make_manager(config)
        await mgr.initialize()
        yield mgr
        await mgr.cleanup()

    @pytest.mark.asyncio
    async def test_initialization(self, config):
        """Test manager initialization."""
        manager = self._make_manager(config)
        await manager.initialize()

        assert manager.is_initialized
        assert manager._sessions == {}

        await manager.cleanup()

    @pytest.mark.asyncio
    async def test_create_session(self, manager):
        """Test creating a session."""
        session_id = "test-session-123"
        task = "Research API providers"
        user_id = "user-456"

        manager.create_session(
            session_id=session_id,
            task=task,
            user_id=user_id
        )

        # Verify session was created. SessionData is now an attribute-based
        # object (not a dict); user_id is used only for path construction and is
        # no longer retained on the session record.
        assert session_id in manager._sessions
        session_data = manager._sessions[session_id]

        assert session_data.memory.session_id == session_id
        assert session_data.memory.task == task
        assert session_data.phase_manager is not None
        assert session_data.context_retriever is not None

    @pytest.mark.asyncio
    async def test_create_duplicate_session(self, manager):
        """Test creating a duplicate session is rejected.

        The manager now fails fast on a duplicate session_id by raising
        ValueError (rather than silently no-op'ing). Either way the invariant is
        the same: a duplicate must not create a second session record.
        """
        session_id = "test-session-123"

        # Create first session
        manager.create_session(session_id=session_id, task="Task 1")

        # Creating a duplicate must not create a second session
        with pytest.raises(ValueError):
            manager.create_session(session_id=session_id, task="Task 2")

        # Should still only have one session
        assert len(manager._sessions) == 1

    @pytest.mark.asyncio
    async def test_add_step_memory(self, manager):
        """Test adding step memory."""
        session_id = "test-session-123"
        manager.create_session(session_id=session_id, task="Research task")

        brain_state = {
            "phase": "discovery",
            "memory": "Started research",
            "next_goal": "Find providers"
        }

        manager.add_step_memory(
            session_id=session_id,
            step=1,
            brain_state=brain_state,
            action_summary="goto(example.com)",
            finding="Found homepage",
            total_steps=50
        )

        # Verify step was added
        session_data = manager._sessions[session_id]
        memory = session_data.memory

        assert memory.current_phase == "discovery"
        assert len(memory.recent_steps) == 1
        assert memory.recent_steps[0].step == 1
        assert memory.progress == "1/50"

    @pytest.mark.asyncio
    async def test_add_step_to_nonexistent_session(self, manager):
        """Test adding step to non-existent session logs error."""
        # Should not crash
        manager.add_step_memory(
            session_id="nonexistent",
            step=1,
            brain_state={"phase": "discovery"},
            action_summary="action",
            finding="finding"
        )

    @pytest.mark.asyncio
    async def test_add_multiple_steps(self, manager):
        """Test adding multiple steps."""
        session_id = "test-session-123"
        manager.create_session(session_id=session_id, task="Research task")

        for i in range(10):
            brain_state = {"phase": "discovery"}
            manager.add_step_memory(
                session_id=session_id,
                step=i + 1,
                brain_state=brain_state,
                action_summary=f"action_{i}",
                finding=f"finding_{i}",
                total_steps=50
            )

        # Verify all steps were added
        memory = manager._sessions[session_id].memory
        assert len(memory.recent_steps) == 10
        assert memory.progress == "10/50"

    @pytest.mark.asyncio
    async def test_phase_transition_tracking(self, manager):
        """Test phase transitions are tracked."""
        session_id = "test-session-123"
        manager.create_session(session_id=session_id, task="Research task")

        # Add steps in discovery
        for i in range(5):
            manager.add_step_memory(
                session_id=session_id,
                step=i + 1,
                brain_state={"phase": "discovery"},
                action_summary=f"action_{i}",
                finding=f"finding_{i}"
            )

        # Add steps in collection (transition!)
        for i in range(5):
            manager.add_step_memory(
                session_id=session_id,
                step=i + 6,
                brain_state={"phase": "collection"},
                action_summary=f"action_{i}",
                finding=f"finding_{i}"
            )

        memory = manager._sessions[session_id].memory

        # Verify transition occurred
        assert memory.current_phase == "collection"
        assert "discovery" in memory.phases_completed
        assert "discovery" in memory.phase_index
        assert "collection" in memory.phase_index

        assert memory.get_phase_by_name("discovery").status == "completed"
        assert memory.get_phase_by_name("collection").status == "active"

    @pytest.mark.asyncio
    async def test_get_context_injection(self, manager):
        """Test getting context injection."""
        session_id = "test-session-123"
        manager.create_session(session_id=session_id, task="Research task")

        # Add some steps
        for i in range(5):
            manager.add_step_memory(
                session_id=session_id,
                step=i + 1,
                brain_state={"phase": "discovery"},
                action_summary=f"action_{i}",
                finding=f"finding_{i}"
            )

        # Get context
        context = manager.get_context_injection(session_id)

        assert context is not None
        assert "test-session-123" in context
        assert "Research task" in context
        assert "discovery" in context.lower()

    @pytest.mark.asyncio
    async def test_get_context_injection_nonexistent_session(self, manager):
        """Test getting context for non-existent session returns None."""
        context = manager.get_context_injection("nonexistent")
        assert context is None

    @pytest.mark.asyncio
    async def test_save_session(self, manager, config):
        """Test saving session to disk."""
        session_id = "test-session-123"
        user_id = "user-456"

        manager.create_session(session_id=session_id, task="Research task", user_id=user_id)

        # Add some data
        for i in range(3):
            manager.add_step_memory(
                session_id=session_id,
                step=i + 1,
                brain_state={"phase": "discovery"},
                action_summary=f"action_{i}",
                finding=f"finding_{i}"
            )

        # Save
        manager.save_session(session_id=session_id, user_id=user_id)

        # Verify file was created
        data_dir = Path(config.data.get("DATA_DIR"))
        session_dir = data_dir / "auto" / user_id / "sessions" / session_id
        memory_file = session_dir / "hierarchical_memory.json"

        assert memory_file.exists()

    @pytest.mark.asyncio
    async def test_save_session_without_user_id(self, manager):
        """Test saving session without user_id."""
        session_id = "test-session-123"
        manager.create_session(session_id=session_id, task="Research task")

        # Should log warning but not crash
        manager.save_session(session_id=session_id, user_id=None)

    @pytest.mark.asyncio
    async def test_save_nonexistent_session(self, manager):
        """Test saving non-existent session logs error."""
        # Should not crash
        manager.save_session(session_id="nonexistent", user_id="user-123")

    @pytest.mark.asyncio
    async def test_load_session(self, manager, config):
        """Test loading session from disk."""
        session_id = "test-session-123"
        user_id = "user-456"

        # Create and save session
        manager.create_session(session_id=session_id, task="Research task", user_id=user_id)
        manager.add_step_memory(
            session_id=session_id,
            step=1,
            brain_state={"phase": "discovery"},
            action_summary="action_1",
            finding="finding_1"
        )
        manager.save_session(session_id=session_id, user_id=user_id)

        # Clear session from memory
        del manager._sessions[session_id]

        # Load session (now returns the loaded HierarchicalMemory, or None)
        loaded = manager.load_session(session_id=session_id, user_id=user_id)

        assert loaded is not None
        assert session_id in manager._sessions

        # Verify data was loaded
        memory = manager._sessions[session_id].memory
        assert memory.task == "Research task"
        assert len(memory.recent_steps) == 1

    @pytest.mark.asyncio
    async def test_load_nonexistent_session(self, manager):
        """Test loading non-existent session returns None."""
        loaded = manager.load_session(session_id="nonexistent", user_id="user-123")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_session_lifecycle(self, manager, config):
        """Test full session lifecycle."""
        session_id = "test-session-123"
        user_id = "user-456"

        # 1. Create session
        manager.create_session(session_id=session_id, task="Full lifecycle test", user_id=user_id)
        assert session_id in manager._sessions

        # 2. Add steps through multiple phases
        for i in range(5):
            manager.add_step_memory(
                session_id=session_id,
                step=i + 1,
                brain_state={"phase": "discovery"},
                action_summary=f"discovery_action_{i}",
                finding=f"discovery_finding_{i}"
            )

        for i in range(5):
            manager.add_step_memory(
                session_id=session_id,
                step=i + 6,
                brain_state={"phase": "collection"},
                action_summary=f"collection_action_{i}",
                finding=f"collection_finding_{i}"
            )

        # 3. Get context
        context = manager.get_context_injection(session_id)
        assert "collection" in context.lower()
        assert "discovery" in context.lower()

        # 4. Save session
        manager.save_session(session_id=session_id, user_id=user_id)

        # 5. Clear from memory
        original_memory = manager._sessions[session_id].memory.model_dump()
        del manager._sessions[session_id]

        # 6. Load session (returns the loaded HierarchicalMemory, or None)
        loaded = manager.load_session(session_id=session_id, user_id=user_id)
        assert loaded is not None

        # 7. Verify integrity
        restored_memory = manager._sessions[session_id].memory
        assert restored_memory.task == "Full lifecycle test"
        assert restored_memory.current_phase == "collection"
        assert len(restored_memory.recent_steps) == 10
        assert "discovery" in restored_memory.phases_completed

    @pytest.mark.asyncio
    async def test_concurrent_sessions(self, manager):
        """Test managing multiple sessions concurrently."""
        # Create 3 sessions
        session_ids = ["session-1", "session-2", "session-3"]
        for sid in session_ids:
            manager.create_session(session_id=sid, task=f"Task for {sid}")

        # Add steps to each
        for sid in session_ids:
            for i in range(3):
                manager.add_step_memory(
                    session_id=sid,
                    step=i + 1,
                    brain_state={"phase": "discovery"},
                    action_summary=f"action_{i}",
                    finding=f"finding_{i}"
                )

        # Verify all sessions exist and are independent
        assert len(manager._sessions) == 3

        for sid in session_ids:
            memory = manager._sessions[sid].memory
            assert memory.session_id == sid
            assert memory.task == f"Task for {sid}"
            assert len(memory.recent_steps) == 3

    @pytest.mark.asyncio
    async def test_cleanup(self, config):
        """Test manager cleanup."""
        manager = self._make_manager(config)
        await manager.initialize()

        # Create some sessions
        manager.create_session(session_id="session-1", task="Task 1")
        manager.create_session(session_id="session-2", task="Task 2")

        assert len(manager._sessions) == 2

        # Cleanup
        await manager.cleanup()

        # Sessions should be cleared
        assert len(manager._sessions) == 0
        assert not manager.is_initialized
