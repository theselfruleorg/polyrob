"""Integration tests for session path isolation.

These tests verify that:
1. Sessions are properly isolated by user_id
2. WebView correctly extracts user_id from JWT tokens
3. PathManager auto-discovery works correctly
4. Session paths are not accessible across users
"""
import pytest
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Test configuration
TEST_DATA_ROOT = None


@pytest.fixture(scope="function")
def test_data_root():
    """Create a temporary data root for testing."""
    global TEST_DATA_ROOT
    with tempfile.TemporaryDirectory() as tmpdir:
        TEST_DATA_ROOT = tmpdir
        yield Path(tmpdir)
        TEST_DATA_ROOT = None


@pytest.fixture(scope="function")
def path_manager(test_data_root):
    """Create a PathManager instance with test data root.

    SessionManager computes all of its session paths through the process-global
    pm() singleton (not a base_dir-derived layout), so to keep SessionManager and
    this PathManager pointing at the SAME tmpdir we install this instance as the
    global pm() for the duration of the test and restore the previous one after.
    """
    from agents.task.path import PathManager
    import agents.task.path as path_module
    from agents.task.path import set_path_manager

    previous = path_module._INSTANCE
    pm_instance = PathManager(data_root=str(test_data_root))
    set_path_manager(pm_instance)
    try:
        yield pm_instance
    finally:
        set_path_manager(previous)


@pytest.fixture(scope="function")
def session_manager(test_data_root, path_manager):
    """Create a SessionManager instance with test data root.

    Depends on the path_manager fixture so the global pm() singleton already
    points at the test tmpdir before SessionManager (which delegates path
    computation to pm()) is constructed.
    """
    from agents.task.agent.session import SessionManager
    return SessionManager(base_dir=str(test_data_root))


class TestPathManagerIsolation:
    """Test PathManager user isolation."""

    def test_session_path_isolation(self, path_manager):
        """Verify sessions are properly isolated by user_id."""
        user1 = "usr_test_user_1"
        user2 = "usr_test_user_2"
        session_id = "test_session_123"

        # Get paths for same session but different users
        path1 = path_manager.get_session_root(session_id, user_id=user1)
        path2 = path_manager.get_session_root(session_id, user_id=user2)

        # Verify isolation
        assert str(path1).startswith(str(path_manager.data_root / user1))
        assert str(path2).startswith(str(path_manager.data_root / user2))
        assert path1 != path2

        # Verify no cross-contamination
        assert user2 not in str(path1)
        assert user1 not in str(path2)

    def test_workspace_dir_isolation(self, path_manager):
        """Verify workspace directories are isolated by user."""
        user1 = "usr_test_user_1"
        user2 = "usr_test_user_2"
        session_id = "test_session_456"

        # Get workspace dirs
        ws1 = path_manager.get_workspace_dir(session_id, user_id=user1)
        ws2 = path_manager.get_workspace_dir(session_id, user_id=user2)

        # Verify isolation
        assert ws1 != ws2
        assert user1 in str(ws1)
        assert user2 in str(ws2)
        assert user2 not in str(ws1)
        assert user1 not in str(ws2)

    def test_feed_dir_isolation(self, path_manager):
        """Verify feed directories are isolated by user."""
        user1 = "usr_test_user_1"
        user2 = "usr_test_user_2"
        session_id = "test_session_789"

        # Get feed dirs
        feed1 = path_manager.get_feed_dir(session_id, user_id=user1)
        feed2 = path_manager.get_feed_dir(session_id, user_id=user2)

        # Verify isolation
        assert feed1 != feed2
        assert user1 in str(feed1)
        assert user2 in str(feed2)


class TestSessionManagerIsolation:
    """Test SessionManager user isolation."""

    def test_create_session_with_user_id(self, session_manager, path_manager):
        """Verify session creation properly stores user_id."""
        user_id = "usr_test_user_1"
        session_id = session_manager.create_session(user_id=user_id)

        # Verify session directory exists
        session_dir = path_manager.get_session_root(session_id, user_id=user_id)
        assert session_dir.exists()
        assert user_id in str(session_dir)

        # Verify metadata contains user_id
        metadata_file = session_dir / "metadata.json"
        assert metadata_file.exists()

        with open(metadata_file) as f:
            metadata = json.load(f)
            assert metadata["user_id"] == user_id
            assert metadata["id"] == session_id

    def test_multiple_users_same_session_id_prefix(self, session_manager, path_manager):
        """Verify different users can have sessions with similar IDs."""
        user1 = "usr_test_user_1"
        user2 = "usr_test_user_2"

        # Create sessions with similar prefixes
        session1 = session_manager.create_session(session_id="test_abc123", user_id=user1)
        session2 = session_manager.create_session(session_id="test_abc456", user_id=user2)

        # Get paths
        path1 = path_manager.get_session_root(session1, user_id=user1)
        path2 = path_manager.get_session_root(session2, user_id=user2)

        # Verify isolation despite similar session IDs
        assert path1 != path2
        assert user1 in str(path1)
        assert user2 in str(path2)


class TestPathManagerAutoDiscovery:
    """Test PathManager auto-discovery functionality."""

    def test_auto_discovery_with_metadata(self, session_manager, path_manager):
        """Verify auto-discovery works when metadata exists."""
        user_id = "usr_test_user_1"
        session_id = session_manager.create_session(user_id=user_id)

        # Discover user without providing user_id
        discovered_user = path_manager._discover_user_for_session(session_id)

        assert discovered_user == user_id

    def test_auto_discovery_caching(self, session_manager, path_manager):
        """Verify auto-discovery results are cached."""
        user_id = "usr_test_user_1"
        session_id = session_manager.create_session(user_id=user_id)

        # First discovery - miss cache
        discovered1 = path_manager._discover_user_for_session(session_id)
        assert discovered1 == user_id

        # Second discovery - hit cache
        discovered2 = path_manager._discover_user_for_session(session_id)
        assert discovered2 == user_id
        assert discovered1 == discovered2

        # Verify cache was used (both calls should return same object)
        assert path_manager._user_session_cache.get(session_id) == user_id

    def test_auto_discovery_failure_for_nonexistent_session(self, path_manager):
        """Verify auto-discovery returns None for non-existent sessions."""
        nonexistent_session = "nonexistent_session_12345"

        discovered_user = path_manager._discover_user_for_session(nonexistent_session)

        assert discovered_user is None

    def test_get_session_root_raises_on_ambiguous_session(self, session_manager, path_manager):
        """Verify PathManager raises error when session exists but user cannot be determined."""
        user_id = "usr_test_user_1"
        session_id = session_manager.create_session(user_id=user_id)

        # Remove metadata to simulate corruption
        session_dir = path_manager.get_session_root(session_id, user_id=user_id)
        metadata_file = session_dir / "metadata.json"
        metadata_file.unlink()

        # Clear cache to force discovery
        path_manager._user_session_cache.clear()

        # Try to get session root without user_id - should raise
        with pytest.raises(RuntimeError, match="user_id cannot be determined"):
            path_manager.get_session_root(session_id, user_id=None)

    def test_retry_logic_on_race_condition(self, path_manager, test_data_root):
        """Verify retry logic handles race conditions during session creation."""
        user_id = "usr_test_user_1"
        session_id = "race_condition_session"

        # Create session directory but delay metadata write (simulates race)
        session_dir = test_data_root / user_id / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # Try to get session root without user_id - should retry and eventually fail
        # (or succeed if metadata appears during retry)
        with pytest.raises(RuntimeError, match="user_id cannot be determined"):
            path_manager.get_session_root(session_id, user_id=None)


class TestWebViewUserExtraction:
    """Test WebView correctly extracts user_id from JWT."""

    @pytest.mark.skipif(
        not os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "..", "webview")),
        reason="WebView not available in this environment"
    )
    def test_webview_workspace_tree_uses_user_id(self, session_manager, path_manager):
        """Verify webview workspace/tree endpoint uses user_id from JWT."""
        from fastapi.testclient import TestClient
        from webview.server import _fastapi
        import jwt as pyjwt

        # Skip if JWT_SECRET_KEY not set
        jwt_secret = os.environ.get("JWT_SECRET_KEY")
        if not jwt_secret:
            pytest.skip("JWT_SECRET_KEY not configured")

        user_id = "usr_test_user_1"
        session_id = session_manager.create_session(user_id=user_id)

        # Create test workspace files
        workspace = path_manager.get_workspace_dir(session_id, user_id=user_id)
        (workspace / "test.txt").write_text("test content")

        # Create JWT token
        token = pyjwt.encode(
            {"sub": "0xTEST", "user_id": user_id, "tier": "admin"},
            jwt_secret,
            algorithm="HS256"
        )

        client = TestClient(_fastapi)

        # Request workspace tree with auth
        response = client.get(
            f"/api/session/{session_id}/workspace/tree",
            headers={"Authorization": f"Bearer {token}"}
        )

        # Should succeed (200) not 404
        assert response.status_code == 200
        data = response.json()
        assert data["type"] == "dir"
        # Should find the test file
        assert len(data["children"]) > 0


class TestSessionIDCleaning:
    """Test session ID cleaning and normalization."""

    def test_clean_session_id_removes_agent_prefix(self, path_manager):
        """Verify agent prefixes are removed during cleaning."""
        # Test various agent prefixes
        test_cases = [
            ("executor_7bc99947-2cb3-45ea-ab24-153c4e993c9e", "7bc99947-2cb3-45ea-ab24-153c4e993c9e"),
            ("planner_abc123-def456", "abc123-def456"),
            ("agent_executor_test123", "test123"),
        ]

        for dirty_id, expected_clean_id in test_cases:
            clean_id = path_manager.clean_session_id(dirty_id)
            assert clean_id == expected_clean_id

    def test_clean_session_id_rejects_dangerous_patterns(self, path_manager):
        """Verify dangerous patterns are rejected."""
        dangerous_ids = [
            "../../../etc/passwd",
            "session/../../secrets",
            "test;rm -rf /",
            "test|cat /etc/passwd",
            "test$(whoami)",
        ]

        for dangerous_id in dangerous_ids:
            with pytest.raises(ValueError, match="Security violation"):
                path_manager.clean_session_id(dangerous_id)

    def test_clean_user_id_preserves_underscores(self, path_manager):
        """Verify user ID cleaning preserves underscores."""
        test_ids = [
            "usr_test_user_1",
            "_anonymous_",
            "usr_144639f1e1754bff",
        ]

        for test_id in test_ids:
            clean_id = path_manager.clean_user_id(test_id)
            # Should preserve underscores
            assert "_" in clean_id or test_id == clean_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
