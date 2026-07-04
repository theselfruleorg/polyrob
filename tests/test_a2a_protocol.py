"""Tests for A2A Protocol Implementation.

Tests cover:
- Agent Card generation and discovery
- Task lifecycle (create, get, send, cancel)
- State mapping between ROB and A2A
- Streaming events
- A2A Client functionality
"""

import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

# Import A2A models
from api.a2a.models import (
    A2ATaskState, A2ATask, A2ATaskStatus, A2AMessage,
    A2AArtifact, SendMessageRequest, JSONRPCRequest,
    JSONRPCResponse, TextPart
)
from api.a2a.task_handler import (
    A2ATaskHandler, ROB_TO_A2A_STATE, A2A_TO_ROB_STATE
)
from api.a2a.agent_card import build_agent_card, AgentSkill, AgentCard


# =============================================================================
# Agent Card Tests
# =============================================================================

class TestAgentCard:
    """Tests for Agent Card generation."""

    def test_build_agent_card_defaults(self):
        """Test Agent Card is built with correct defaults."""
        card = build_agent_card()

        from core.version import get_version

        assert card.name == "POLYROB"
        assert card.protocolVersion == "1.0"
        assert card.version == get_version()
        # Instance-neutral default: no A2A_BASE_URL/request → local base URL
        assert card.url == "http://localhost:9000/a2a"

    def test_agent_card_has_required_skills(self):
        """Test Agent Card includes expected skills."""
        card = build_agent_card()

        skill_ids = [s.id for s in card.skills]

        assert "web-automation" in skill_ids
        assert "file-management" in skill_ids
        assert "research" in skill_ids
        assert "mcp-integration" in skill_ids
        assert "task-planning" in skill_ids

    def test_agent_card_has_security_schemes(self):
        """Test Agent Card includes security schemes."""
        card = build_agent_card()

        assert "x402" in card.securitySchemes
        assert "bearer" in card.securitySchemes
        assert "apiKey" in card.securitySchemes

    def test_agent_card_auth_primary_is_api_key(self):
        """Test apiKey is the primary advertised authentication, x402 an alternative.

        TEST-DRIFT FIX: the advertised auth ordering was intentionally restructured.
        x402 used to be listed first ("primary"); the current contract lists apiKey
        first (the code comments it as "Recommended: API key (simple, persistent)")
        with x402 as an alternative ("pay-per-request, no account"). x402 is NOT
        removed — it is still an advertised security requirement, just no longer the
        primary one. We assert the current ordering and that x402 is still offered.
        """
        card = build_agent_card()

        # apiKey is now the primary (first) security requirement.
        assert len(card.security) > 0
        assert "apiKey" in card.security[0]

        # x402 is still advertised as an alternative auth requirement.
        all_schemes = set()
        for requirement in card.security:
            all_schemes.update(requirement.keys())
        assert "x402" in all_schemes, "x402 should still be an advertised auth option"

    def test_agent_card_capabilities(self):
        """Test Agent Card capabilities are correct."""
        card = build_agent_card()

        assert card.capabilities.streaming is True
        assert card.capabilities.pushNotifications is True
        assert card.capabilities.stateTransitionHistory is True

    def test_agent_card_pricing_info(self):
        """Test Agent Card includes pricing information (incl. x402 pricing).

        TEST-DRIFT FIX: the pricing schema was intentionally restructured. x402
        pricing used to live at `pricing["x402"]` with an `enabled` flag; it now
        lives under `pricing["authentication_options"]["x402"]` (alongside
        `api_key`), and the top level carries `model`/`description`/`credits`.
        x402 pricing (including `per_request_usd`) is still present.
        """
        card = build_agent_card()

        assert card.pricing is not None
        assert card.pricing["model"] == "pay-per-request"

        # x402 pricing is now nested under authentication_options.
        x402_pricing = card.pricing["authentication_options"]["x402"]
        assert "per_request_usd" in x402_pricing


# =============================================================================
# State Mapping Tests
# =============================================================================

class TestStateMappings:
    """Tests for status/state mappings."""

    def test_rob_to_a2a_state_mapping(self):
        """Test ROB session status maps correctly to A2A task state."""
        assert ROB_TO_A2A_STATE["created"] == A2ATaskState.SUBMITTED
        assert ROB_TO_A2A_STATE["running"] == A2ATaskState.WORKING
        assert ROB_TO_A2A_STATE["completed"] == A2ATaskState.COMPLETED
        assert ROB_TO_A2A_STATE["suspended"] == A2ATaskState.INPUT_REQUIRED
        assert ROB_TO_A2A_STATE["failed"] == A2ATaskState.FAILED
        assert ROB_TO_A2A_STATE["cancelled"] == A2ATaskState.CANCELED

    def test_a2a_to_rob_state_mapping(self):
        """Test A2A task state maps correctly to ROB session status."""
        assert A2A_TO_ROB_STATE[A2ATaskState.SUBMITTED] == "created"
        assert A2A_TO_ROB_STATE[A2ATaskState.WORKING] == "running"
        assert A2A_TO_ROB_STATE[A2ATaskState.COMPLETED] == "completed"
        assert A2A_TO_ROB_STATE[A2ATaskState.INPUT_REQUIRED] == "suspended"
        assert A2A_TO_ROB_STATE[A2ATaskState.FAILED] == "failed"
        assert A2A_TO_ROB_STATE[A2ATaskState.CANCELED] == "cancelled"

    def test_terminal_states(self):
        """Test terminal state detection."""
        assert A2ATaskState.is_terminal(A2ATaskState.COMPLETED) is True
        assert A2ATaskState.is_terminal(A2ATaskState.FAILED) is True
        assert A2ATaskState.is_terminal(A2ATaskState.CANCELED) is True
        assert A2ATaskState.is_terminal(A2ATaskState.REJECTED) is True

        assert A2ATaskState.is_terminal(A2ATaskState.WORKING) is False
        assert A2ATaskState.is_terminal(A2ATaskState.SUBMITTED) is False
        assert A2ATaskState.is_terminal(A2ATaskState.INPUT_REQUIRED) is False

    def test_interrupted_states(self):
        """Test interrupted state detection."""
        assert A2ATaskState.is_interrupted(A2ATaskState.INPUT_REQUIRED) is True
        assert A2ATaskState.is_interrupted(A2ATaskState.AUTH_REQUIRED) is True

        assert A2ATaskState.is_interrupted(A2ATaskState.WORKING) is False
        assert A2ATaskState.is_interrupted(A2ATaskState.COMPLETED) is False


# =============================================================================
# Task Handler Tests
# =============================================================================

class TestA2ATaskHandler:
    """Tests for A2A Task Handler."""

    @pytest.fixture
    def mock_container(self):
        """Create mock dependency container."""
        container = MagicMock()

        # Mock TaskAgent
        task_agent = AsyncMock()
        task_agent.session_manager = MagicMock()
        task_agent._active_orchestrators = {}

        container.get_agent.return_value = task_agent
        container.get_service.return_value = task_agent.session_manager

        return container

    @pytest.fixture
    def handler(self, mock_container):
        """Create handler with mock container."""
        return A2ATaskHandler(mock_container)

    def test_extract_text_from_message(self, handler):
        """Test text extraction from A2A message."""
        message = A2AMessage(
            role="user",
            parts=[
                {"kind": "text", "text": "Hello"},
                {"kind": "text", "text": "World"},
                {"kind": "file", "file": {"uri": "http://example.com/file.txt"}}
            ]
        )

        text = handler._extract_text_from_message(message)
        assert text == "Hello\nWorld"

    def test_extract_files_from_message(self, handler):
        """Test file extraction from A2A message."""
        message = A2AMessage(
            role="user",
            parts=[
                {"kind": "text", "text": "Hello"},
                {"kind": "file", "file": {"uri": "http://example.com/file.txt"}},
                {"kind": "data", "data": {"key": "value"}}
            ]
        )

        files = handler._extract_files_from_message(message)
        assert len(files) == 2
        assert files[0]["kind"] == "file"
        assert files[1]["kind"] == "data"

    def test_extract_images_from_message(self, handler):
        """Test image extraction for vision processing."""
        message = A2AMessage(
            role="user",
            parts=[
                {"kind": "text", "text": "Analyze this image"},
                {
                    "kind": "file",
                    "file": {
                        "bytes": "base64encodeddata",
                        "mimeType": "image/png"
                    }
                }
            ]
        )

        images = handler._extract_images_from_message(message)
        assert len(images) == 1
        assert images[0]["type"] == "image_url"
        assert "data:image/png;base64" in images[0]["image_url"]["url"]

    def test_session_status_to_a2a_state(self, handler):
        """Test session status to A2A state conversion."""
        assert handler._session_status_to_a2a_state("running") == A2ATaskState.WORKING
        assert handler._session_status_to_a2a_state("COMPLETED") == A2ATaskState.COMPLETED
        assert handler._session_status_to_a2a_state("unknown_status") == A2ATaskState.UNKNOWN

    @pytest.mark.asyncio
    async def test_create_task(self, handler, mock_container):
        """Test task creation."""
        # Setup mock
        task_agent = mock_container.get_agent.return_value
        task_agent.create_session = AsyncMock(return_value={
            "id": "session-123",
            "status": "created"
        })

        # Create request
        message = A2AMessage(
            role="user",
            parts=[{"kind": "text", "text": "Take a screenshot of google.com"}]
        )
        request = SendMessageRequest(message=message)

        # Execute
        task = await handler.create_task(request, "user-1")

        # Verify
        assert task.id == "session-123"
        assert task.status.state == A2ATaskState.SUBMITTED
        task_agent.create_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_task(self, handler, mock_container):
        """Test getting task status."""
        # Setup mock
        task_agent = mock_container.get_agent.return_value
        task_agent.get_session_by_id = AsyncMock(return_value={
            "id": "session-123",
            "status": "running",
            "user_id": "user-1",
            "task": "Test task",
            "created_at": datetime.now().isoformat()
        })

        # Execute
        task = await handler.get_task("session-123")

        # Verify
        assert task.id == "session-123"
        assert task.status.state == A2ATaskState.WORKING

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, handler, mock_container):
        """Test getting non-existent task."""
        task_agent = mock_container.get_agent.return_value
        task_agent.get_session_by_id = AsyncMock(return_value=None)

        with pytest.raises(ValueError, match="not found"):
            await handler.get_task("non-existent")

    @pytest.mark.asyncio
    async def test_cancel_task(self, handler, mock_container):
        """Test task cancellation."""
        task_agent = mock_container.get_agent.return_value
        task_agent.get_session_by_id = AsyncMock(return_value={
            "id": "session-123",
            "status": "running",
            "user_id": "user-1"
        })
        task_agent.cancel_session = AsyncMock(return_value=True)

        # Mock get_task for final status
        with patch.object(handler, 'get_task', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = A2ATask(
                id="session-123",
                contextId="user-1",
                status=A2ATaskStatus(state=A2ATaskState.CANCELED)
            )

            task = await handler.cancel_task("session-123", "user-1")

            assert task.status.state == A2ATaskState.CANCELED
            task_agent.cancel_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_terminal_task_fails(self, handler, mock_container):
        """Test canceling already completed task fails."""
        task_agent = mock_container.get_agent.return_value
        task_agent.get_session_by_id = AsyncMock(return_value={
            "id": "session-123",
            "status": "completed",
            "user_id": "user-1"
        })

        with pytest.raises(ValueError, match="terminal state"):
            await handler.cancel_task("session-123", "user-1")


# =============================================================================
# JSON-RPC Tests
# =============================================================================

class TestJSONRPC:
    """Tests for JSON-RPC request/response handling."""

    def test_jsonrpc_request_format(self):
        """Test JSON-RPC request model."""
        request = JSONRPCRequest(
            method="message/send",
            params={"message": {"role": "user", "parts": []}},
            id=1
        )

        assert request.jsonrpc == "2.0"
        assert request.method == "message/send"
        assert request.id == 1

    def test_jsonrpc_response_success(self):
        """Test successful JSON-RPC response."""
        response = JSONRPCResponse(
            result={"id": "task-123", "state": "working"},
            id=1
        )

        assert response.error is None
        assert response.result["id"] == "task-123"

    def test_jsonrpc_response_error(self):
        """Test error JSON-RPC response."""
        from api.a2a.models import JSONRPCError

        response = JSONRPCResponse(
            error=JSONRPCError(
                code=-32000,
                message="Task not found"
            ),
            id=1
        )

        assert response.result is None
        assert response.error.code == -32000


# =============================================================================
# A2A Client Tests
# =============================================================================

class TestA2AClient:
    """Tests for A2A Client functionality."""

    @pytest.fixture
    def client(self):
        """Create A2A client for testing."""
        from api.a2a.client import A2AClient
        return A2AClient()

    def test_build_headers_with_token(self, client):
        """Test header building with auth token."""
        headers = client._build_headers("test-token")

        assert headers["Authorization"] == "Bearer test-token"
        assert headers["Content-Type"] == "application/json"

    def test_build_headers_without_token(self, client):
        """Test header building without auth token."""
        headers = client._build_headers(None)

        assert "Authorization" not in headers
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_discover_agent_caches_result(self, client):
        """Test agent discovery caches results."""
        mock_card = {
            "name": "Test Agent",
            "url": "https://test.example.com/a2a",
            "version": "1.0.0",
            "protocolVersion": "1.0",
            "capabilities": {"streaming": True},
            "skills": [],
            "provider": {"organization": "Test", "url": "https://test.example.com"},
            "securitySchemes": {},
            "security": [],
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"]
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_response = MagicMock()
            mock_response.json.return_value = mock_card
            mock_response.raise_for_status = MagicMock()

            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )

            # First call - should fetch
            card1 = await client.discover_agent("https://test.example.com")
            assert card1.name == "Test Agent"

            # Second call - should use cache
            card2 = await client.discover_agent("https://test.example.com")
            assert card2.name == "Test Agent"

            # Cache should have entry
            assert "https://test.example.com" in client._agent_cache

    def test_get_cached_agents(self, client):
        """Test retrieving cached agents."""
        # Initially empty
        assert len(client.get_cached_agents()) == 0

        # Add to cache manually for testing
        from api.a2a.client import A2AAgentInfo
        client._agent_cache["https://test.com"] = A2AAgentInfo(
            url="https://test.com",
            card=MagicMock(),
            discovered_at=datetime.now().isoformat()
        )

        assert len(client.get_cached_agents()) == 1

    def test_clear_cache(self, client):
        """Test cache clearing."""
        from api.a2a.client import A2AAgentInfo
        client._agent_cache["https://test.com"] = A2AAgentInfo(
            url="https://test.com",
            card=MagicMock(),
            discovered_at=datetime.now().isoformat()
        )

        client.clear_cache()
        assert len(client._agent_cache) == 0


# =============================================================================
# Model Tests
# =============================================================================

class TestA2AModels:
    """Tests for A2A data models."""

    def test_a2a_task_creation(self):
        """Test A2ATask model creation."""
        task = A2ATask(
            id="task-123",
            contextId="ctx-456",
            status=A2ATaskStatus(state=A2ATaskState.WORKING)
        )

        assert task.id == "task-123"
        assert task.contextId == "ctx-456"
        assert task.kind == "task"
        assert task.status.state == A2ATaskState.WORKING

    def test_a2a_message_creation(self):
        """Test A2AMessage model creation."""
        message = A2AMessage(
            role="user",
            parts=[{"kind": "text", "text": "Hello"}]
        )

        assert message.role == "user"
        assert message.kind == "message"
        assert len(message.parts) == 1
        assert message.messageId is not None  # Auto-generated

    def test_a2a_artifact_creation(self):
        """Test A2AArtifact model creation."""
        artifact = A2AArtifact(
            name="output.txt",
            parts=[{"kind": "text", "text": "Result data"}],
            description="Task output"
        )

        assert artifact.name == "output.txt"
        assert artifact.artifactId is not None  # Auto-generated
        assert artifact.description == "Task output"

    def test_send_message_request(self):
        """Test SendMessageRequest model."""
        message = A2AMessage(
            role="user",
            parts=[{"kind": "text", "text": "Test task"}]
        )
        request = SendMessageRequest(
            message=message,
            configuration={"pushNotificationConfig": {"url": "https://hook.example.com"}}
        )

        assert request.message.role == "user"
        assert "pushNotificationConfig" in request.configuration


# =============================================================================
# Integration Tests (require actual services)
# =============================================================================

@pytest.mark.integration
class TestA2AIntegration:
    """Integration tests requiring running services.

    Run with: pytest -m integration tests/test_a2a_protocol.py
    """

    @pytest.mark.asyncio
    async def test_full_task_lifecycle(self):
        """Test complete task lifecycle: create -> get -> complete."""
        # This would test against a running instance
        # Skipped by default due to @pytest.mark.integration
        pytest.skip("Requires running server")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
