"""A2A Task Handler - Maps A2A tasks to POLYROB sessions.

This module handles the mapping between A2A protocol concepts
and POLYROB's internal session management:

A2A Concept     → POLYROB Equivalent
---------------------------------
Task            → Session
TaskState       → SessionStatus
Message         → UserMessage/feed events
Artifact        → Workspace files
contextId       → user_id grouping
"""

import logging
import os
from typing import Optional, Dict, Any, List, Tuple

from core.env import bool_env as _bool_env
from core.exceptions import SessionOwnershipError
from datetime import datetime
import uuid
import json
import asyncio

from api.dependencies import resolve_orchestrator

from api.a2a.models import (
    A2ATaskState, A2ATask, A2ATaskStatus, A2AMessage, A2AArtifact,
    SendMessageRequest, Part, TextPart, FilePart, DataPart,
    PushNotificationConfig
)
from agents.task.agent.session import SessionStatus

logger = logging.getLogger(__name__)

# H2 FIX: hold strong references to fire-and-forget session tasks. The event loop
# keeps only a weak reference to a bare asyncio.Task, so a task whose only reference
# was the create_task() return value can be garbage-collected mid-run and silently
# cancelled. These tasks run entire agent sessions, so that is a real session-death risk.
_BACKGROUND_SESSION_TASKS: set = set()


def _spawn_session_task(coro) -> "asyncio.Task":
    """Schedule a background coroutine while retaining a strong reference to it."""
    task = asyncio.create_task(coro)
    _BACKGROUND_SESSION_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_SESSION_TASKS.discard)
    return task


def _current_activity(session_id: str):
    """019 P4: the session's live RunActivity snapshot (or None). Fail-open."""
    try:
        from agents.task.telemetry.run_activity import get_activity
        return get_activity(session_id)
    except Exception:
        return None


# =============================================================================
# Status Mapping
# =============================================================================

# Map POLYROB SessionStatus to A2A TaskState
# Note: "paused" was removed from SessionStatus - use cancelled for user interruption
ROB_TO_A2A_STATE: Dict[str, A2ATaskState] = {
    "created": A2ATaskState.SUBMITTED,
    "running": A2ATaskState.WORKING,
    "resumed": A2ATaskState.WORKING,
    "completed": A2ATaskState.COMPLETED,
    "suspended": A2ATaskState.INPUT_REQUIRED,
    "failed": A2ATaskState.FAILED,
    "error": A2ATaskState.FAILED,
    "cancelled": A2ATaskState.CANCELED,
}

# Map A2A TaskState to POLYROB SessionStatus
A2A_TO_ROB_STATE: Dict[A2ATaskState, str] = {
    A2ATaskState.SUBMITTED: "created",
    A2ATaskState.WORKING: "running",
    A2ATaskState.INPUT_REQUIRED: "suspended",
    A2ATaskState.AUTH_REQUIRED: "suspended",
    A2ATaskState.COMPLETED: "completed",
    A2ATaskState.FAILED: "failed",
    A2ATaskState.CANCELED: "cancelled",
    A2ATaskState.REJECTED: "cancelled",
}


# =============================================================================
# Task Handler
# =============================================================================

class A2ATaskHandler:
    """Handles A2A task operations by delegating to TaskAgent."""

    def __init__(self, container):
        """Initialize handler with dependency container.

        Args:
            container: DependencyContainer with TaskAgent and services
        """
        self.container = container
        self.logger = logging.getLogger("a2a.task_handler")

        # Cache for push notification configs
        self._push_configs: Dict[str, PushNotificationConfig] = {}

    def _get_task_agent(self):
        """Get TaskAgent from container."""
        return self.container.get_agent("task_agent")

    def _get_session_manager(self):
        """Get SessionManager from container or TaskAgent."""
        sm = self.container.get_service("session_manager")
        if not sm:
            agent = self._get_task_agent()
            if agent:
                sm = agent.session_manager
        return sm

    # =========================================================================
    # Message/Part Extraction
    # =========================================================================

    def _extract_text_from_message(self, message: A2AMessage) -> str:
        """Extract text content from A2A message parts.

        Args:
            message: A2A message with parts

        Returns:
            Concatenated text from all text parts
        """
        text_parts = []
        for part in message.parts:
            # Handle both dict and model formats
            if isinstance(part, dict):
                if part.get("kind") == "text" or "text" in part:
                    text_parts.append(part.get("text", ""))
            elif hasattr(part, 'text'):
                text_parts.append(part.text)
        text = "\n".join(filter(None, text_parts))

        # C1/A3: opt-in context-reference expansion for remote A2A intake.
        # Filesystem refs are refused (no trusted session workspace here); only @url
        # expands, behind the SSRF guard. Fails soft — never breaks task creation.
        if text:
            from agents.task.constants import AutonomyConfig
            if AutonomyConfig.context_references_enabled():
                try:
                    from agents.task.agent.messages.context_references import (
                        preprocess_context_references,
                    )
                    text = preprocess_context_references(
                        text, confine_to_root=True, allow_filesystem=False
                    )
                except Exception as e:
                    self.logger.debug(f"context-ref expansion skipped: {e}")
        return text

    def _extract_files_from_message(self, message: A2AMessage) -> List[Dict[str, Any]]:
        """Extract file parts from A2A message.

        Args:
            message: A2A message with parts

        Returns:
            List of file part dictionaries
        """
        files = []
        for part in message.parts:
            if isinstance(part, dict):
                if part.get("kind") in ["file", "data"]:
                    files.append(part)
            elif hasattr(part, 'kind') and part.kind in ["file", "data"]:
                files.append(part.dict() if hasattr(part, 'dict') else part)
        return files

    def _extract_images_from_message(self, message: A2AMessage) -> List[Dict[str, Any]]:
        """Extract image attachments for vision processing.

        Args:
            message: A2A message with parts

        Returns:
            List of image data dicts compatible with vision models
        """
        images = []
        for part in message.parts:
            part_dict = part if isinstance(part, dict) else (part.dict() if hasattr(part, 'dict') else {})

            if part_dict.get("kind") == "file":
                file_data = part_dict.get("file", {})
                mime_type = file_data.get("mimeType", "")

                # Check if it's an image
                if mime_type.startswith("image/"):
                    if "bytes" in file_data:
                        # Inline base64 image
                        images.append({
                            'type': 'image_url',
                            'image_url': {
                                'url': f'data:{mime_type};base64,{file_data["bytes"]}'
                            }
                        })
                    elif "uri" in file_data:
                        # URI reference
                        images.append({
                            'type': 'image_url',
                            'image_url': {
                                'url': file_data["uri"]
                            }
                        })
        return images

    # =========================================================================
    # State Conversion
    # =========================================================================

    def _session_status_to_a2a_state(self, status: str) -> A2ATaskState:
        """Convert POLYROB session status to A2A task state.

        Args:
            status: POLYROB session status string

        Returns:
            Corresponding A2ATaskState
        """
        return ROB_TO_A2A_STATE.get(
            status.lower() if status else "unknown",
            A2ATaskState.UNKNOWN
        )

    def _a2a_state_to_session_status(self, state: A2ATaskState) -> str:
        """Convert A2A task state to POLYROB session status.

        Args:
            state: A2A task state

        Returns:
            Corresponding POLYROB session status string
        """
        return A2A_TO_ROB_STATE.get(state, "unknown")

    # =========================================================================
    # Task Operations
    # =========================================================================

    async def create_task(
        self,
        request: SendMessageRequest,
        user_id: str,
        context_id: Optional[str] = None
    ) -> A2ATask:
        """Create a new A2A task (maps to POLYROB session).

        Args:
            request: SendMessageRequest with initial message
            user_id: User/payer identifier
            context_id: Optional context ID for grouping

        Returns:
            Created A2ATask

        Raises:
            RuntimeError: If TaskAgent unavailable
            ValueError: If message has no text content
        """
        agent = self._get_task_agent()
        if not agent:
            raise RuntimeError("TaskAgent not available")

        # Extract task text from message
        task_text = self._extract_text_from_message(request.message)
        if not task_text:
            raise ValueError("No text content in message")

        # Extract images for vision
        image_attachments = self._extract_images_from_message(request.message)

        # Get configuration from request
        config = request.configuration or {}
        metadata = request.message.metadata or {}

        # Prepare session config
        from agents.task_agent_lite import SessionRequest
        session_request = SessionRequest(
            task=task_text,
            model=metadata.get("model", config.get("model", "gpt-5")),
            provider=metadata.get("provider", config.get("provider", "openai")),
            tools=metadata.get("tools", config.get("tools", ["browser", "filesystem"])),
            max_steps=metadata.get("max_steps", config.get("max_steps", 50)),
            use_vision=metadata.get("use_vision", config.get("use_vision", True))
        )

        # Allow client to specify task ID
        task_id = request.message.taskId

        # Create session
        try:
            session_info = await agent.create_session(
                user_id=user_id,
                request=session_request,
                session_id=task_id
            )
        except SessionOwnershipError:
            # C4: client-supplied taskId belongs to another tenant. Stay
            # probe-resistant (mirror _authorize_owner: never a distinguishable 403).
            raise ValueError(f"Task {task_id} could not be created")

        session_id = session_info["id"]

        # Store context ID mapping
        if context_id or request.message.contextId:
            ctx_id = context_id or request.message.contextId
            sm = self._get_session_manager()
            if sm:
                sm.update_session_metadata(session_id, {
                    "a2a_context_id": ctx_id
                })

        # Store push notification config if provided
        push_config = config.get("pushNotificationConfig")
        if push_config:
            self._push_configs[session_id] = PushNotificationConfig(**push_config)
            sm = self._get_session_manager()
            if sm:
                sm.update_session_metadata(session_id, {
                    "a2a_push_url": push_config.get("url")
                })

        # Queue initial message with images if present
        orchestrator = await resolve_orchestrator(session_id, agent)
        if orchestrator and image_attachments:
            try:
                await orchestrator.submit_user_message(
                    agent_id=None,
                    text=task_text,
                    kind="a2a_initial",
                    metadata={"image_attachments": image_attachments}
                )
                self.logger.info(f"Queued initial message with {len(image_attachments)} images")
            except Exception as e:
                self.logger.error(f"Failed to queue initial message: {e}")

        # Start session execution in background (strong ref retained — see
        # _spawn_session_task). Wrapped so the registered webhook (if any) is
        # notified when the run settles (T4-11).
        _spawn_session_task(self._run_session_with_push(agent, user_id, session_id))

        # Build A2A task response
        return A2ATask(
            id=session_id,
            contextId=context_id or request.message.contextId or user_id,
            status=A2ATaskStatus(
                state=A2ATaskState.SUBMITTED,
                timestamp=datetime.now().isoformat()
            ),
            metadata={
                "session_id": session_id,
                "task": task_text,
                "created_at": datetime.now().isoformat(),
                "model": session_request.model,
                "tools": session_request.tools
            }
        )

    @staticmethod
    def _authorize_owner(session_info: dict, task_id: str, user_id: Optional[str]) -> None:
        """Enforce task ownership. Raises not-found (never a distinguishable 403)
        when ``user_id`` is supplied and does not own the session, so a caller
        cannot use the error to probe another tenant's task-IDs. ``user_id=None``
        means the caller already authorized (internal re-fetch) — no check."""
        if user_id is not None and session_info.get("user_id") != user_id:
            raise ValueError(f"Task {task_id} not found")

    async def get_task(
        self,
        task_id: str,
        history_length: Optional[int] = None,
        user_id: Optional[str] = None
    ) -> A2ATask:
        """Get current status of an A2A task.

        Args:
            task_id: Task/session ID
            history_length: Number of history messages to include
                           None = all, 0 = none, >0 = last N

        Returns:
            A2ATask with current status

        Raises:
            ValueError: If task not found
        """
        agent = self._get_task_agent()
        if not agent:
            raise RuntimeError("TaskAgent not available")

        session_info = await agent.get_session_by_id(task_id)
        if not session_info:
            raise ValueError(f"Task {task_id} not found")
        self._authorize_owner(session_info, task_id, user_id)

        # Map status to A2A state
        rob_status = session_info.get("status", "unknown")
        a2a_state = self._session_status_to_a2a_state(rob_status)

        # Collect artifacts if completed
        artifacts = []
        if A2ATaskState.is_terminal(a2a_state):
            artifacts = await self._collect_artifacts(task_id, session_info)

        # Build history if requested
        history = None
        if history_length is None or history_length > 0:
            history = await self._build_history(task_id, session_info, history_length)

        # Get context ID
        context_id = session_info.get("metadata", {}).get("a2a_context_id")
        if not context_id:
            context_id = session_info.get("user_id", task_id)

        return A2ATask(
            id=task_id,
            contextId=context_id,
            status=A2ATaskStatus(
                state=a2a_state,
                timestamp=session_info.get("updated_at", datetime.now().isoformat())
            ),
            history=history,
            artifacts=artifacts,
            metadata={
                "session_id": task_id,
                "internal_status": rob_status,
                "task": session_info.get("task"),
                "created_at": session_info.get("created_at"),
                "updated_at": session_info.get("updated_at"),
                "model": session_info.get("config", {}).get("model"),
                "tools": session_info.get("config", {}).get("tools", []),
                # 019 P4: live run activity ({phase, detail, seconds_in_state,
                # step, call_id}) from the in-process RunActivity snapshot;
                # None when unknown/remote — same semantics as the plain
                # session-status API's current_activity field.
                "current_activity": _current_activity(task_id)
            }
        )

    async def send_message(
        self,
        task_id: str,
        message: A2AMessage,
        user_id: str
    ) -> A2ATask:
        """Send a message to an existing A2A task.

        Args:
            task_id: Task/session ID
            message: Message to send
            user_id: User identifier

        Returns:
            Updated A2ATask

        Raises:
            ValueError: If task not found or in terminal state
        """
        agent = self._get_task_agent()
        if not agent:
            raise RuntimeError("TaskAgent not available")

        session_info = await agent.get_session_by_id(task_id)
        if not session_info:
            raise ValueError(f"Task {task_id} not found")
        # Ownership guard BEFORE any write/inject/resume: a non-owner must not be
        # able to inject a message into or resume another tenant's session.
        self._authorize_owner(session_info, task_id, user_id)

        # Check if task is in terminal state
        current_state = self._session_status_to_a2a_state(
            session_info.get("status", "unknown")
        )
        if A2ATaskState.is_terminal(current_state):
            raise ValueError(
                f"Cannot send message to task in terminal state: {current_state.value}"
            )

        # Extract text and images
        text = self._extract_text_from_message(message)
        image_attachments = self._extract_images_from_message(message)

        # Get orchestrator and queue message
        orchestrator = await resolve_orchestrator(task_id, agent)
        if orchestrator:
            metadata = message.metadata or {}
            if image_attachments:
                metadata["image_attachments"] = image_attachments

            await orchestrator.submit_user_message(
                agent_id=None,
                text=text,
                kind="a2a_message",
                metadata=metadata
            )
            self.logger.info(f"Queued message to task {task_id}")

        # Resume if suspended
        status = session_info.get("status", "").lower()
        if status in ["completed", "suspended", "failed", "error"]:
            self.logger.info(f"Resuming task {task_id} from {status}")
            _spawn_session_task(self._run_session_with_push(
                agent,
                session_info.get("user_id"),
                task_id
            ))

        return await self.get_task(task_id)

    async def cancel_task(self, task_id: str, user_id: str) -> A2ATask:
        """Cancel an A2A task.

        Args:
            task_id: Task/session ID
            user_id: User identifier

        Returns:
            Updated A2ATask with canceled state

        Raises:
            ValueError: If task not found or already terminal
        """
        agent = self._get_task_agent()
        if not agent:
            raise RuntimeError("TaskAgent not available")

        session_info = await agent.get_session_by_id(task_id)
        if not session_info:
            raise ValueError(f"Task {task_id} not found")

        # Check if already terminal
        current_state = self._session_status_to_a2a_state(
            session_info.get("status", "unknown")
        )
        if A2ATaskState.is_terminal(current_state):
            raise ValueError(
                f"Cannot cancel task in terminal state: {current_state.value}"
            )

        # Cancel via TaskAgent
        success = await agent.cancel_session(
            user_id=user_id,
            session_id=task_id,
            force=True
        )

        if not success:
            raise ValueError(f"Failed to cancel task {task_id}")

        # T4-11: the cancel outcome is known here — push CANCELED directly to the
        # registered webhook (fail-open; a webhook failure never breaks the cancel).
        try:
            await self.send_push_notification(task_id, A2ATaskState.CANCELED)
        except Exception as e:
            self.logger.debug(f"push notify on cancel skipped: {e}")

        return await self.get_task(task_id)

    async def list_tasks(
        self,
        user_id: str,
        context_id: Optional[str] = None,
        page_token: Optional[str] = None,
        page_size: int = 20
    ) -> Tuple[List[A2ATask], Optional[str]]:
        """List tasks for a user.

        Args:
            user_id: User identifier
            context_id: Optional context filter
            page_token: Pagination token
            page_size: Number of results per page

        Returns:
            Tuple of (tasks, next_page_token)
        """
        sm = self._get_session_manager()
        if not sm:
            return [], None

        # Get all sessions
        all_sessions = sm._sessions

        # Filter by user
        user_sessions = [
            (sid, info) for sid, info in all_sessions.items()
            if info.get("user_id") == user_id
        ]

        # Filter by context if specified
        if context_id:
            user_sessions = [
                (sid, info) for sid, info in user_sessions
                if info.get("metadata", {}).get("a2a_context_id") == context_id
            ]

        # Sort by creation time (newest first)
        user_sessions.sort(
            key=lambda x: x[1].get("created_at", ""),
            reverse=True
        )

        # Handle pagination
        start_idx = 0
        if page_token:
            try:
                start_idx = int(page_token)
            except ValueError:
                start_idx = 0

        end_idx = start_idx + page_size
        page_sessions = user_sessions[start_idx:end_idx]

        # Build tasks
        tasks = []
        for session_id, session_info in page_sessions:
            try:
                task = await self.get_task(session_id, history_length=0)
                tasks.append(task)
            except Exception as e:
                self.logger.warning(f"Failed to build task for {session_id}: {e}")

        # Calculate next page token
        next_token = None
        if end_idx < len(user_sessions):
            next_token = str(end_idx)

        return tasks, next_token

    # =========================================================================
    # Push Notifications
    # =========================================================================

    async def _authorize_task(self, task_id: str, user_id: Optional[str]) -> None:
        """Ownership gate for push-config ops: the caller must own ``task_id``.

        Mirrors ``_authorize_owner`` (raises not-found, never a distinguishable 403,
        so a caller can't probe another tenant's task ids). ``user_id=None`` means
        the caller already authorized (internal use) — no check.
        """
        if user_id is None:
            return
        agent = self._get_task_agent()
        if not agent:
            raise RuntimeError("TaskAgent not available")
        session_info = await agent.get_session_by_id(task_id)
        if not session_info:
            raise ValueError(f"Task {task_id} not found")
        self._authorize_owner(session_info, task_id, user_id)

    async def set_push_notification_config(
        self,
        task_id: str,
        config: PushNotificationConfig,
        user_id: Optional[str] = None
    ) -> bool:
        """Set push notification config for a task.

        Args:
            task_id: Task/session ID
            config: Push notification configuration
            user_id: Authenticated caller — must own the task (IDOR gate)

        Returns:
            True if successful
        """
        await self._authorize_task(task_id, user_id)

        self._push_configs[task_id] = config

        sm = self._get_session_manager()
        if sm:
            sm.update_session_metadata(task_id, {
                "a2a_push_url": config.url,
                "a2a_push_token": config.token
            })

        return True

    async def get_push_notification_config(
        self,
        task_id: str,
        user_id: Optional[str] = None
    ) -> Optional[PushNotificationConfig]:
        """Get push notification config for a task (caller must own the task)."""
        await self._authorize_task(task_id, user_id)
        return self._push_configs.get(task_id)

    async def delete_push_notification_config(
        self,
        task_id: str,
        user_id: Optional[str] = None
    ) -> bool:
        """Delete push notification config for a task (caller must own the task)."""
        await self._authorize_task(task_id, user_id)
        if task_id in self._push_configs:
            del self._push_configs[task_id]
            return True
        return False

    def _get_push_config(
        self,
        task_id: str,
        session_info: Optional[Dict[str, Any]] = None
    ) -> Optional[PushNotificationConfig]:
        """Resolve the push config: in-memory first, then the session-metadata
        mirror (restart-survivable — the in-memory dict dies with the process
        while the metadata written at registration does not). T4-11."""
        config = self._push_configs.get(task_id)
        if config:
            return config
        try:
            meta = (session_info or {}).get("metadata") or {}
            url = meta.get("a2a_push_url")
            if url:
                return PushNotificationConfig(url=url, token=meta.get("a2a_push_token"))
        except Exception:
            pass
        return None

    async def _notify_push_state(
        self,
        task_id: str,
        message: Optional[str] = None
    ) -> bool:
        """Push the task's CURRENT state to its registered webhook (T4-11).

        Refetches the session so the delivered state reflects the real outcome
        (completed/failed/suspended). No-op (False) when no webhook is
        registered. Fail-open — a webhook failure never breaks the task."""
        try:
            agent = self._get_task_agent()
            session_info = await agent.get_session_by_id(task_id) if agent else None
            if not session_info:
                return False
            if self._get_push_config(task_id, session_info) is None:
                return False
            state = self._session_status_to_a2a_state(
                session_info.get("status", "unknown"))
            return await self.send_push_notification(task_id, state, message)
        except Exception as e:
            self.logger.debug(f"push state notify skipped for {task_id}: {e}")
            return False

    async def _run_session_with_push(self, agent, user_id: str, session_id: str) -> None:
        """Run the session and, when it settles (normally or by crash), deliver
        the terminal state to the registered webhook (T4-11 — the agent card
        advertises pushNotifications, so a registered client must hear back)."""
        try:
            await agent.run_session(user_id, session_id)
        finally:
            try:
                await self._notify_push_state(session_id)
            except Exception as e:  # never mask the run outcome
                self.logger.debug(f"push notify after run skipped: {e}")

    async def send_push_notification(
        self,
        task_id: str,
        state: A2ATaskState,
        message: Optional[str] = None
    ) -> bool:
        """Send push notification for task update.

        Args:
            task_id: Task/session ID
            state: Current task state
            message: Optional status message

        Returns:
            True if notification sent successfully
        """
        config = self._push_configs.get(task_id)
        if not config:
            # Restart-survivable fallback: rebuild from the metadata mirror.
            try:
                agent = self._get_task_agent()
                session_info = await agent.get_session_by_id(task_id) if agent else None
                config = self._get_push_config(task_id, session_info)
            except Exception:
                config = None
        if not config:
            return False

        try:
            import httpx

            payload = {
                "taskId": task_id,
                "state": state.value,
                "timestamp": datetime.now().isoformat()
            }
            if message:
                payload["message"] = message

            headers = {}
            if config.token:
                headers["X-A2A-Notification-Token"] = config.token
            if config.authentication:
                if "Bearer" in config.authentication.schemes:
                    headers["Authorization"] = f"Bearer {config.authentication.credentials}"

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    config.url,
                    json=payload,
                    headers=headers
                )
                response.raise_for_status()

            self.logger.info(f"Sent push notification for task {task_id}: {state.value}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to send push notification: {e}")
            return False

    # =========================================================================
    # Helper Methods
    # =========================================================================

    async def _collect_artifacts(
        self,
        task_id: str,
        session_info: Dict[str, Any]
    ) -> List[A2AArtifact]:
        """Collect artifacts from session workspace.

        Args:
            task_id: Task/session ID
            session_info: Session metadata

        Returns:
            List of A2AArtifacts from workspace
        """
        from agents.task.path import pm
        import mimetypes

        artifacts = []
        user_id = session_info.get("user_id")

        workspace_dir = pm().get_workspace_dir(task_id, user_id)
        if not workspace_dir or not workspace_dir.exists():
            return artifacts

        for file_path in workspace_dir.rglob("*"):
            if file_path.is_file():
                mime_type, _ = mimetypes.guess_type(str(file_path))
                relative_path = str(file_path.relative_to(workspace_dir))

                artifacts.append(A2AArtifact(
                    artifactId=str(uuid.uuid4()),
                    name=file_path.name,
                    description=f"Generated file: {relative_path}",
                    parts=[{
                        "kind": "file",
                        "file": {
                            "uri": f"/api/task/sessions/{task_id}/workspace/{relative_path}",
                            "name": file_path.name,
                            "mimeType": mime_type or "application/octet-stream"
                        }
                    }],
                    metadata={
                        "path": relative_path,
                        "size": file_path.stat().st_size
                    }
                ))

        return artifacts

    async def _build_history(
        self,
        task_id: str,
        session_info: Dict[str, Any],
        limit: Optional[int] = None
    ) -> List[A2AMessage]:
        """Build message history from session feed.

        Args:
            task_id: Task/session ID
            session_info: Session metadata
            limit: Max messages to return (None = all)

        Returns:
            List of A2AMessages
        """
        from agents.task.path import pm

        messages = []
        user_id = session_info.get("user_id")

        feed_dir = pm().get_subdir(task_id, "feed", user_id=user_id)
        if not feed_dir or not feed_dir.exists():
            return messages

        # Collect feed files
        feed_files = sorted(feed_dir.glob("*.json"))

        # Apply limit
        if limit and limit > 0:
            feed_files = feed_files[-limit:]

        for feed_file in feed_files:
            try:
                with open(feed_file) as f:
                    event = json.load(f)

                event_type = event.get("type", "")
                event_data = event.get("data", {})

                # Convert to A2A message based on event type
                if event_type in ["user_message", "message"]:
                    messages.append(A2AMessage(
                        messageId=str(uuid.uuid4()),
                        role="user",
                        parts=[{"kind": "text", "text": event_data.get("text", "")}],
                        metadata=event_data.get("metadata")
                    ))
                elif event_type in ["agent_message", "step_result", "action_result"]:
                    text = event_data.get("text") or event_data.get("result", "")
                    if text:
                        messages.append(A2AMessage(
                            messageId=str(uuid.uuid4()),
                            role="agent",
                            parts=[{"kind": "text", "text": str(text)}],
                            metadata={"event_type": event_type}
                        ))

            except Exception as e:
                self.logger.warning(f"Failed to parse feed file {feed_file}: {e}")

        return messages
