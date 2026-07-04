"""A2A Client - Consume services from other A2A agents.

This module enables POLYROB to act as an A2A client, discovering
and delegating tasks to other A2A-compliant agents.

Use Cases:
- Delegate specialized tasks to domain-specific agents
- Compose workflows across multiple agents
- Access capabilities not available locally

Reference: https://a2a-protocol.org/latest/specification/
"""

import logging
from typing import Optional, Dict, Any, List, AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timedelta
import httpx
import json
import asyncio

from api.a2a.models import (
    A2ATask, A2ATaskState, A2ATaskStatus,
    A2AMessage, A2AArtifact, SendMessageRequest,
    JSONRPCRequest, JSONRPCResponse,
    TaskStatusUpdateEvent
)
from api.a2a.agent_card import AgentCard

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class A2AAgentInfo:
    """Cached information about a discovered A2A agent."""
    url: str
    card: AgentCard
    discovered_at: str  # ISO timestamp
    last_used_at: Optional[str] = None
    success_count: int = 0
    failure_count: int = 0


@dataclass
class A2AClientConfig:
    """Configuration for A2A client."""
    timeout: float = 30.0  # Request timeout in seconds
    stream_timeout: float = 300.0  # Streaming timeout
    max_retries: int = 3
    retry_delay: float = 1.0
    cache_ttl: int = 3600  # Agent card cache TTL in seconds


# =============================================================================
# A2A Client
# =============================================================================

class A2AClient:
    """Client for interacting with other A2A agents.

    Supports:
    - Agent discovery via Agent Card
    - Task creation and management
    - Streaming task updates
    - Push notification configuration

    Example:
        client = A2AClient()

        # Discover agent
        card = await client.discover_agent("https://agent.example.com")

        # Create task
        task = await client.send_message(
            agent_url="https://agent.example.com",
            text="Analyze this document",
            auth_token="your-token"
        )

        # Stream updates
        async for event in client.stream_task(
            agent_url="https://agent.example.com",
            task_id=task.id,
            auth_token="your-token"
        ):
            print(f"Status: {event.task.status.state}")
    """

    def __init__(self, config: Optional[A2AClientConfig] = None):
        """Initialize A2A client.

        Args:
            config: Optional client configuration
        """
        self.config = config or A2AClientConfig()
        self.logger = logging.getLogger("a2a.client")

        # Cache discovered agents
        self._agent_cache: Dict[str, A2AAgentInfo] = {}

    def _is_cache_stale(self, info: A2AAgentInfo) -> bool:
        """Check if a cached agent entry is stale based on TTL.

        Args:
            info: Cached agent info

        Returns:
            True if cache entry is stale and should be refreshed
        """
        try:
            discovered_at = datetime.fromisoformat(info.discovered_at)
            age = datetime.now() - discovered_at
            return age > timedelta(seconds=self.config.cache_ttl)
        except (ValueError, TypeError):
            # Invalid timestamp - treat as stale
            return True

    # =========================================================================
    # Agent Discovery
    # =========================================================================

    async def discover_agent(
        self,
        agent_url: str,
        force_refresh: bool = False
    ) -> AgentCard:
        """Discover an A2A agent by fetching its Agent Card.

        Args:
            agent_url: Base URL of the agent
            force_refresh: Bypass cache

        Returns:
            AgentCard with agent capabilities

        Raises:
            httpx.HTTPError: Network or HTTP error
            ValueError: Invalid Agent Card
        """
        # Check cache (including TTL)
        if not force_refresh and agent_url in self._agent_cache:
            cached = self._agent_cache[agent_url]
            if not self._is_cache_stale(cached):
                return cached.card
            # Cache is stale - will refresh below

        # Normalize URL
        base_url = agent_url.rstrip("/")

        # Try well-known path first (RFC 8615)
        card_url = f"{base_url}/.well-known/agent.json"

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            try:
                response = await client.get(card_url)
                response.raise_for_status()
            except httpx.HTTPError:
                # Try alternative path
                card_url = f"{base_url}/a2a/agent-card"
                response = await client.get(card_url)
                response.raise_for_status()

            card_data = response.json()

        # Parse and validate
        try:
            card = AgentCard(**card_data)
        except Exception as e:
            raise ValueError(f"Invalid Agent Card: {e}")

        # Cache agent info
        from datetime import datetime
        self._agent_cache[agent_url] = A2AAgentInfo(
            url=agent_url,
            card=card,
            discovered_at=datetime.now().isoformat()
        )

        self.logger.info(f"Discovered agent: {card.name} at {agent_url}")
        return card

    async def find_agents_with_skill(
        self,
        skill_id: str,
        known_agents: Optional[List[str]] = None
    ) -> List[A2AAgentInfo]:
        """Find agents that have a specific skill.

        Args:
            skill_id: Skill ID to search for
            known_agents: List of agent URLs to search

        Returns:
            List of agents with the requested skill
        """
        matching = []

        # Search cached agents
        for url, info in self._agent_cache.items():
            for skill in info.card.skills:
                if skill.id == skill_id:
                    matching.append(info)
                    break

        # Search known agents not in cache
        if known_agents:
            for url in known_agents:
                if url not in self._agent_cache:
                    try:
                        card = await self.discover_agent(url)
                        for skill in card.skills:
                            if skill.id == skill_id:
                                matching.append(self._agent_cache[url])
                                break
                    except Exception as e:
                        self.logger.warning(f"Failed to discover {url}: {e}")

        return matching

    # =========================================================================
    # Task Operations
    # =========================================================================

    async def send_message(
        self,
        agent_url: str,
        text: str,
        auth_token: Optional[str] = None,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
        files: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> A2ATask:
        """Send a message to create or continue a task.

        Args:
            agent_url: Agent base URL
            text: Message text
            auth_token: Authentication token
            task_id: Optional existing task ID (to continue)
            context_id: Optional context ID
            files: Optional file attachments
            metadata: Optional message metadata

        Returns:
            A2ATask with current status

        Raises:
            httpx.HTTPError: Network error
            ValueError: Invalid response
        """
        base_url = agent_url.rstrip("/")

        # Build message parts
        parts = [{"kind": "text", "text": text}]

        # Add file parts
        if files:
            for file_info in files:
                parts.append({
                    "kind": "file",
                    "file": file_info
                })

        # Build message
        import uuid
        message = {
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "parts": parts
        }
        if task_id:
            message["taskId"] = task_id
        if context_id:
            message["contextId"] = context_id
        if metadata:
            message["metadata"] = metadata

        # Build JSON-RPC request
        rpc_request = JSONRPCRequest(
            method="message/send",
            params={"message": message},
            id=1
        )

        # Make request
        headers = self._build_headers(auth_token)

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.post(
                f"{base_url}/a2a/rpc",
                json=rpc_request.dict(),
                headers=headers
            )
            response.raise_for_status()

            rpc_response = JSONRPCResponse(**response.json())

        if rpc_response.error:
            raise ValueError(f"RPC error: {rpc_response.error.message}")

        # Parse task from response
        return A2ATask(**rpc_response.result)

    async def get_task(
        self,
        agent_url: str,
        task_id: str,
        auth_token: Optional[str] = None,
        history_length: Optional[int] = None
    ) -> A2ATask:
        """Get task status from a remote agent.

        Args:
            agent_url: Agent base URL
            task_id: Task ID
            auth_token: Authentication token
            history_length: Number of history messages to include

        Returns:
            A2ATask with current status
        """
        base_url = agent_url.rstrip("/")

        params = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length

        rpc_request = JSONRPCRequest(
            method="tasks/get",
            params=params,
            id=1
        )

        headers = self._build_headers(auth_token)

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.post(
                f"{base_url}/a2a/rpc",
                json=rpc_request.dict(),
                headers=headers
            )
            response.raise_for_status()

            rpc_response = JSONRPCResponse(**response.json())

        if rpc_response.error:
            raise ValueError(f"RPC error: {rpc_response.error.message}")

        return A2ATask(**rpc_response.result)

    async def cancel_task(
        self,
        agent_url: str,
        task_id: str,
        auth_token: Optional[str] = None
    ) -> A2ATask:
        """Cancel a task on a remote agent.

        Args:
            agent_url: Agent base URL
            task_id: Task ID
            auth_token: Authentication token

        Returns:
            A2ATask with canceled status
        """
        base_url = agent_url.rstrip("/")

        rpc_request = JSONRPCRequest(
            method="tasks/cancel",
            params={"id": task_id},
            id=1
        )

        headers = self._build_headers(auth_token)

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.post(
                f"{base_url}/a2a/rpc",
                json=rpc_request.dict(),
                headers=headers
            )
            response.raise_for_status()

            rpc_response = JSONRPCResponse(**response.json())

        if rpc_response.error:
            raise ValueError(f"RPC error: {rpc_response.error.message}")

        return A2ATask(**rpc_response.result)

    # =========================================================================
    # Streaming
    # =========================================================================

    async def stream_task(
        self,
        agent_url: str,
        task_id: str,
        auth_token: Optional[str] = None,
        history_length: Optional[int] = None
    ) -> AsyncGenerator[TaskStatusUpdateEvent, None]:
        """Stream task updates from a remote agent.

        Args:
            agent_url: Agent base URL
            task_id: Task ID
            auth_token: Authentication token
            history_length: Number of events to replay

        Yields:
            TaskStatusUpdateEvent for each update
        """
        base_url = agent_url.rstrip("/")
        headers = self._build_headers(auth_token)

        url = f"{base_url}/a2a/tasks/{task_id}/stream"
        if history_length is not None:
            url += f"?historyLength={history_length}"

        async with httpx.AsyncClient(timeout=self.config.stream_timeout) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    # Parse SSE format
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        try:
                            data = json.loads(data_str)
                            rpc_response = JSONRPCResponse(**data)

                            if rpc_response.error:
                                self.logger.error(
                                    f"Stream error: {rpc_response.error.message}"
                                )
                                continue

                            if rpc_response.result:
                                # Parse as task status update
                                task = A2ATask(**rpc_response.result.get("task", {}))
                                final = rpc_response.result.get("final", False)

                                yield TaskStatusUpdateEvent(
                                    task=task,
                                    final=final
                                )

                                if final:
                                    return

                        except Exception as e:
                            self.logger.warning(f"Failed to parse SSE: {e}")

    async def stream_message(
        self,
        agent_url: str,
        text: str,
        auth_token: Optional[str] = None,
        context_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> AsyncGenerator[TaskStatusUpdateEvent, None]:
        """Send message and stream task updates.

        Combines send_message with streaming response.

        Args:
            agent_url: Agent base URL
            text: Message text
            auth_token: Authentication token
            context_id: Optional context ID
            metadata: Optional message metadata

        Yields:
            TaskStatusUpdateEvent for each update
        """
        base_url = agent_url.rstrip("/")
        headers = self._build_headers(auth_token)

        # Build message
        import uuid
        message = {
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "parts": [{"kind": "text", "text": text}]
        }
        if context_id:
            message["contextId"] = context_id
        if metadata:
            message["metadata"] = metadata

        request_body = {
            "message": message
        }

        async with httpx.AsyncClient(timeout=self.config.stream_timeout) as client:
            async with client.stream(
                "POST",
                f"{base_url}/a2a/message/stream",
                json=request_body,
                headers=headers
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        try:
                            data = json.loads(data_str)
                            rpc_response = JSONRPCResponse(**data)

                            if rpc_response.result:
                                task = A2ATask(**rpc_response.result.get("task", {}))
                                final = rpc_response.result.get("final", False)

                                yield TaskStatusUpdateEvent(
                                    task=task,
                                    final=final
                                )

                                if final:
                                    return

                        except Exception as e:
                            self.logger.warning(f"Failed to parse SSE: {e}")

    # =========================================================================
    # High-Level Operations
    # =========================================================================

    async def delegate_task(
        self,
        agent_url: str,
        task: str,
        auth_token: Optional[str] = None,
        wait_for_completion: bool = True,
        timeout: float = 300.0
    ) -> A2ATask:
        """Delegate a task to another agent and wait for completion.

        High-level convenience method that:
        1. Discovers agent capabilities
        2. Creates task
        3. Waits for completion (optionally)
        4. Returns final result

        Args:
            agent_url: Agent base URL
            task: Task description
            auth_token: Authentication token
            wait_for_completion: Wait for task to complete
            timeout: Maximum wait time in seconds

        Returns:
            A2ATask with final status and artifacts
        """
        # Discover agent first
        await self.discover_agent(agent_url)

        # Create task
        result = await self.send_message(
            agent_url=agent_url,
            text=task,
            auth_token=auth_token
        )

        if not wait_for_completion:
            return result

        # Poll for completion
        task_id = result.id
        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")

            result = await self.get_task(
                agent_url=agent_url,
                task_id=task_id,
                auth_token=auth_token
            )

            if A2ATaskState.is_terminal(result.status.state):
                # Update cache statistics
                if agent_url in self._agent_cache:
                    info = self._agent_cache[agent_url]
                    if result.status.state == A2ATaskState.COMPLETED:
                        info.success_count += 1
                    else:
                        info.failure_count += 1

                return result

            await asyncio.sleep(2)

    # =========================================================================
    # Helpers
    # =========================================================================

    def _build_headers(self, auth_token: Optional[str] = None) -> Dict[str, str]:
        """Build HTTP headers for A2A requests.

        Args:
            auth_token: Optional authentication token

        Returns:
            Headers dictionary
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        return headers

    def get_cached_agents(self) -> List[A2AAgentInfo]:
        """Get list of cached agent information.

        Returns:
            List of discovered agents
        """
        return list(self._agent_cache.values())

    def clear_cache(self) -> None:
        """Clear the agent cache."""
        self._agent_cache.clear()


# =============================================================================
# Module-level singleton
# =============================================================================

_client: Optional[A2AClient] = None


def get_a2a_client() -> A2AClient:
    """Get shared A2A client instance."""
    global _client
    if _client is None:
        _client = A2AClient()
    return _client
