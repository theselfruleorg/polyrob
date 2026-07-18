"""
HTTP API endpoints for Task multi-session support.
Provides RESTful endpoints for session management and user messaging.
"""

from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, Depends, Request, UploadFile, File
from pydantic import BaseModel, Field
import logging
import os
import time
import uuid
from datetime import datetime
import aiofiles

from api.session_routing import guard_remote
import asyncio


logger = logging.getLogger(__name__)

# H2 FIX: hold strong references to fire-and-forget session tasks so the event loop
# can't garbage-collect a running agent session (it keeps only a weak ref to a bare
# Task). Tasks discard themselves on completion via the done callback.
_BACKGROUND_SESSION_TASKS: set = set()


def _spawn_session_task(coro) -> "asyncio.Task":
    """Schedule a background coroutine while retaining a strong reference to it."""
    task = asyncio.create_task(coro)
    _BACKGROUND_SESSION_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_SESSION_TASKS.discard)
    return task

# Create router for Task endpoints
router = APIRouter(prefix="/task", tags=["task"])

# Import models from api.models to avoid duplication
from api.models import (
    SessionStatusResponse,
    MessageResponse,
    UserMessage as UserMessageRequest,
    SessionResponse
)

# Helper to clean session IDs at API entry (single point of cleaning)
def clean_session_id_at_entry(session_id: str) -> str:
    """Clean session ID once at API entry point.

    This is the ONLY place where session IDs should be cleaned in the API layer.
    All downstream code can trust that session IDs are already sanitized.
    """
    from agents.task.path import pm
    return pm().clean_session_id(session_id)

# Helper to normalize status values
def normalize_status_value(value: Any) -> str:
    """Normalize status/state value to lowercase string.

    Handles various input formats:
    - SessionStatus enum objects
    - Enum string representations ("SessionStatus.RUNNING")
    - Plain strings ("running", "RUNNING")
    - None/null values

    Returns:
        Lowercase status string (e.g., "running", "completed")
    """
    # Import here to avoid circular dependency
    from agents.task.agent.session import SessionStatus

    # Handle enum objects
    if isinstance(value, SessionStatus):
        return value.value

    # Handle strings
    if isinstance(value, str):
        # Handle enum string like "SessionStatus.RUNNING" or "SessionState.RUNNING"
        if '.' in value:
            return value.split('.')[-1].lower()
        return value.lower()

    # Handle None or other types
    return str(value).lower() if value else "unknown"

def _require_session_owner(req: Request, resource_owner: Optional[str]) -> None:
    """Raise 403 if the authenticated caller does not own `resource_owner`.

    Mirrors the ownership-check pattern already correct at the
    workspace/upload endpoint (api/task_http_api.py ~1554-1564) — applied
    consistently across the session read/write endpoints that were missing it
    (E8 / A6 gap 4). No-op when resource_owner is falsy (nothing recorded to
    compare against — matches the pre-existing upload-endpoint convention).
    """
    from utils.auth_utils import get_authenticated_user_id
    caller_id = get_authenticated_user_id(req)
    if resource_owner and caller_id != resource_owner:
        raise HTTPException(status_code=403, detail="Access denied - session owned by a different user")

# Dependency to get TaskAgent instance
async def get_task_agent():
    """Get the TaskAgent instance from the container"""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    # Use get_agent for consistency
    agent = container.get_agent("task_agent")
    if not agent:
        # Fallback to get_service for backward compatibility
        agent = container.get_service("task_agent")
    if not agent:
        raise HTTPException(status_code=503, detail="Task agent not available")
    return agent

@router.post("/sessions/{session_id}/messages", response_model=MessageResponse)
async def send_user_message(
    session_id: str,
    request: UserMessageRequest,
    req: Request,
    agent = Depends(get_task_agent)
):
    """
    Send a user message to a running session.

    This endpoint allows sending guidance, corrections, or other messages
    to an active Task session for processing in the next step.
    """
    try:
        # OPTIMIZATION: Clean session ID once at API entry
        session_id = clean_session_id_at_entry(session_id)

        # Item 6: if this session is owned by another worker, return an honest 409
        # (with owner_pid) instead of a false-404. No-op for the in-process registry.
        guard_remote(agent, session_id)

        # Get session from agent
        session_info = await agent.get_session_by_id(session_id)
        if not session_info:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        
        # Check session status (unified field name)
        status = session_info.get('status', 'unknown').lower()

        # Allow messages in resumable states for continuous chat
        # - "created": first message to start the agent with attached files
        # - "running": active session  
        # - "completed/resumed/suspended": continuous chat resume
        # - "failed/error": retry after failure (error is legacy, failed is current)
        RESUMABLE_STATUSES = ["created", "running", "completed", "resumed", "suspended", "failed", "error"]
        if status not in RESUMABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Session is in {status} status, cannot send messages"
            )

        # Get user_id early for use in closure
        user_id = session_info.get('user_id')

        # E8 (A6 gap 4): the caller must own this session — any authenticated
        # caller could otherwise inject a message into another tenant's session.
        _require_session_owner(req, user_id)

        # ============================================================================
        # CRITICAL FIX: Process attached files BEFORE starting session
        # This eliminates race condition where agent starts before message is queued
        # ============================================================================

        # STEP 1: Verify all attached files are ready (explicit synchronization)
        if request.attached_files:
            logger.info(f"📁 Verifying {len(request.attached_files)} attached file(s) are ready...")
            files_ready, missing_files = await verify_files_ready(
                request.attached_files,
                session_id,
                user_id,
                max_wait_seconds=10.0
            )

            if not files_ready:
                logger.error(f"📁 File verification failed. Missing/unreadable: {missing_files}")
                raise HTTPException(
                    status_code=425,  # Too Early
                    detail=f"Files not ready: {', '.join(missing_files)}. Please retry in a few seconds.",
                    headers={"Retry-After": "5"}
                )

            logger.info(f"📁 ✅ All {len(request.attached_files)} file(s) verified and ready")

        # STEP 2: Process attached files to create metadata (BEFORE starting session)
        message_text = request.text
        image_attachments = []  # Track images for vision

        if request.attached_files:
            logger.info(f"📎 Processing {len(request.attached_files)} attached file(s)...")
            for file_path in request.attached_files:
                logger.info(f"📎 Processing file: {file_path}")
                # Returns tuple (text, images)
                message_text, images = await inject_file_content_to_message(
                    file_path,
                    message_text,
                    session_id,
                    user_id
                )
                logger.info(f"📎 File processed: {file_path}")

                # Collect images for vision
                if images:
                    image_attachments.extend(images)
                    logger.info(f"📷 Collected {len(images)} image(s) for vision from {file_path}")

        # STEP 3: Create metadata with images (BEFORE starting session)
        metadata = request.metadata or {}
        if image_attachments:
            metadata['image_attachments'] = image_attachments
            logger.info(f"📷 Prepared message with {len(image_attachments)} image(s) for vision")

        # STEP 4: Get orchestrator (or start session if needed)
        orchestrator = session_info.get('orchestrator')
        if not orchestrator:
            # Look up the live orchestrator via the public accessor (for completed sessions)
            if hasattr(agent, 'get_orchestrator'):
                orchestrator = agent.get_orchestrator(session_id)

        if not orchestrator:
            # Try to get from active sessions (nested structure - for compatibility)
            if user_id and hasattr(agent, 'active_sessions'):
                user_sessions = agent.active_sessions.get(user_id, {})
                session_data = user_sessions.get(session_id, {})
                orchestrator = session_data.get('orchestrator')

        # Check if orchestrator has agents
        has_agents = orchestrator and hasattr(orchestrator, 'agents') and len(orchestrator.agents) > 0
        
        # Track whether message has been queued to prevent double-queueing
        message_queued = False

        # STEP 5: Start session if needed (QUEUE MESSAGE FIRST to prevent race condition)
        if not has_agents and status == "created":
            logger.info(
                f"Starting agent for session {session_id} with pre-queued message "
                f"({len(image_attachments)} image(s) ready)"
            )

            # Start session in background (don't wait for it to be ready)
            import asyncio

            # CRITICAL FIX (Nov 25, 2025): Queue message BEFORE starting agent
            # This prevents race condition where agent starts and checks for messages
            # before the message is queued, causing images to be lost.
            
            # Get the live orchestrator via the public accessor (not from session_info)
            # Note: get_session_by_id() doesn't include orchestrator in its return value
            pre_orchestrator = None
            if hasattr(agent, 'get_orchestrator'):
                pre_orchestrator = agent.get_orchestrator(session_id)
            
            if pre_orchestrator:
                # Queue message FIRST
                logger.info(f"📷 Pre-queuing message with {len(image_attachments)} image(s) BEFORE agent starts")
                await pre_orchestrator.submit_user_message(
                    None,
                    message_text,
                    request.kind,
                    metadata
                )
                message_queued = True  # Mark as queued
                logger.info(f"✅ Message pre-queued successfully")
            else:
                logger.warning(f"Orchestrator not ready for pre-queuing, will queue after start")

            # Capture user_id and session_id in closure
            _user_id = user_id
            _session_id = session_id

            async def run_session_with_logging():
                try:
                    logger.info(f"Starting session execution for {_session_id}")
                    result = await agent.run_session(_user_id, _session_id)
                    logger.info(f"Session {_session_id} completed with result: {result}")
                except Exception as e:
                    logger.error(f"Session {_session_id} failed with error: {e}", exc_info=True)

            # Now start the agent (message is already queued; strong ref retained)
            _spawn_session_task(run_session_with_logging())

            # Give it a tiny moment to start up
            await asyncio.sleep(0.1)

            # Update orchestrator reference
            orchestrator = pre_orchestrator

        elif not orchestrator:
            logger.warning(f"No orchestrator for session {session_id} in status {status}")
            # Fall through to queue message below

        # STEP 6: Queue message (only if not already queued above)
        # Use explicit flag to prevent any double-queueing race conditions
        if not message_queued and orchestrator:
            await orchestrator.submit_user_message(
                None,  # Let orchestrator route to active agent
                message_text,
                request.kind,
                metadata
            )
            message_queued = True

        logger.info(f"Sent user message to session {session_id}: {request.text[:50]}...")

        # NEW: Emit user message to feed for chat display
        # CRITICAL FIX: Skip if running session with attached files - will be added later
        # to prevent duplicate user_message events in the feed
        should_add_to_feed = not (request.attached_files and status == "running" and not message_queued)
        
        if should_add_to_feed and hasattr(agent, 'session_manager'):
            try:
                agent.session_manager.add_to_feed(
                    session_id,
                    'user_message',
                    {
                        'text': request.text,
                        'kind': request.kind,
                        'metadata': request.metadata or {},
                        'timestamp': time.time()
                    }
                )
                logger.debug(f"Added user message to feed for session {session_id}")
            except Exception as e:
                logger.warning(f"Failed to add user message to feed: {e}")

        # Handle terminal states that need resume
        # NOTE: Don't update status here - let TaskAgent handle status transitions
        # to avoid race condition where API and TaskAgent both try to update status
        if status in ["suspended", "failed", "error"]:
            logger.info(f"Session {session_id} is {status}, will be resumed by TaskAgent")

        # Crash-mid-turn recovery: a session interrupted when the process died is
        # rewritten running->"suspended" at startup (SessionManager._load_sessions_from_disk),
        # so it enters the resumable block below. In a FRESH process its live orchestrator
        # is absent, and STEP 6 (which needs an orchestrator) never queued the inbound
        # message — see the ensure_session_and_deliver block below, which recreates the
        # session from disk and delivers the message so the resume actually processes it.

        # If session is in a resumable state, restart execution to process the message
        if status in ["completed", "resumed", "suspended", "failed", "error"]:
            import asyncio
            user_id_from_session = session_info.get('user_id')

            # Deliver the message into the (possibly evicted) session BEFORE running it.
            # STEP 6 only queues when a live orchestrator exists; in a FRESH process the
            # orchestrator is absent, so without this the resume runs with the message
            # never queued — `_run_session_impl` recreates from disk but a completed
            # session with no pending input short-circuits ("No new input"), silently
            # dropping the message. `ensure_session_and_deliver` recreates-then-queues
            # (the self-wake rail), so the recreated session actually processes it.
            # Fail-open: on any delivery error, fall back to the bare run below.
            if not message_queued and not orchestrator and hasattr(agent, 'ensure_session_and_deliver'):
                try:
                    _delivery = await agent.ensure_session_and_deliver(
                        user_id_from_session, session_id, message_text,
                        kind=request.kind, metadata=metadata)
                    if _delivery == "delivered":
                        message_queued = True
                        logger.info(f"Session {session_id} message delivered into recreated orchestrator")
                    elif _delivery == "busy":
                        logger.info(f"Session {session_id} busy — message queued on resident session")
                except Exception as e:
                    logger.warning(f"ensure_session_and_deliver failed for {session_id} "
                                   f"(falling back to bare resume): {e}")

            # NOTE: Status transition is handled by TaskAgent._run_session_impl
            # API should NOT update status to avoid race condition:
            # 1. API reads status='completed'
            # 2. API updates to 'resumed' 
            # 3. TaskAgent tries try_transition_status(completed→resumed) - FAILS!
            # 
            # Instead, let TaskAgent be the single owner of status transitions.
            # API only increments task phase for multi-phase tracking.
            if hasattr(agent, 'session_manager'):
                # ✅ Increment task phase for multi-phase tracking
                try:
                    new_phase = agent.session_manager.increment_task_phase(session_id)
                    logger.info(f"Session {session_id} entering Phase {new_phase} (status transition handled by TaskAgent)")
                except Exception as e:
                    logger.debug(f"Could not increment task_phase: {e}")
                    logger.info(f"Session {session_id} resuming (status transition handled by TaskAgent)")

            # Create background task with error tracking. H2 FIX: retain a strong
            # reference in _BACKGROUND_SESSION_TASKS so the event loop can't GC this
            # session task after the handler returns (the asyncio.shield below only
            # keeps it alive during the 2s wait, not afterward).
            restart_task = asyncio.create_task(
                agent.run_session(user_id_from_session, session_id)
            )
            _BACKGROUND_SESSION_TASKS.add(restart_task)
            restart_task.add_done_callback(_BACKGROUND_SESSION_TASKS.discard)

            # Wait briefly to catch immediate errors (2 seconds)
            resume_status = "running"
            resume_error = None
            is_retryable = False

            try:
                await asyncio.wait_for(
                    asyncio.shield(restart_task),  # Shield so it continues in background
                    timeout=2.0
                )
                resume_status = "running"
                resume_error = None
            except asyncio.TimeoutError:
                # Expected - task running in background
                logger.debug(f"Session {session_id} auto-resume initiated (background)")
                resume_status = "running"
                resume_error = None
            except ValueError as e:
                # User/validation error - permanent failure
                logger.error(f"Auto-resume validation error for {session_id}: {e}")
                resume_status = "failed"
                resume_error = str(e)
                is_retryable = False

                # Emit telemetry
                if hasattr(agent, 'telemetry') and agent.telemetry:
                    try:
                        agent.telemetry.capture_event(
                            event_type="auto_resume_failure",
                            data={
                                "session_id": session_id,
                                "error": str(e),
                                "error_type": "validation",
                                "retryable": False
                            }
                        )
                    except Exception:
                        pass
            except RuntimeError as e:
                # System error - may be retryable
                logger.error(f"Auto-resume system error for {session_id}: {e}")
                resume_status = "error"
                resume_error = str(e)
                is_retryable = True

                # Emit telemetry
                if hasattr(agent, 'telemetry') and agent.telemetry:
                    try:
                        agent.telemetry.capture_event(
                            event_type="auto_resume_failure",
                            data={
                                "session_id": session_id,
                                "error": str(e),
                                "error_type": "runtime",
                                "retryable": True
                            }
                        )
                    except Exception:
                        pass
            except Exception as e:
                # Unknown error - log with traceback
                logger.error(f"Auto-resume unexpected error for {session_id}: {e}", exc_info=True)
                resume_status = "error"
                resume_error = f"Unexpected error: {str(e)}"
                is_retryable = True

                # Emit telemetry
                if hasattr(agent, 'telemetry') and agent.telemetry:
                    try:
                        agent.telemetry.capture_event(
                            event_type="auto_resume_failure",
                            data={
                                "session_id": session_id,
                                "error": str(e),
                                "error_type": type(e).__name__,
                                "retryable": True
                            }
                        )
                    except Exception:
                        pass

            return MessageResponse(
                success=(resume_status == "running"),
                message=f"Message queued, resume: {resume_status}",
                metadata={
                    "session_id": session_id,
                    "resumed": (resume_status == "running"),
                    "resume_status": resume_status,
                    "resume_error": resume_error,
                    "retryable": is_retryable  # Hint for client retry logic
                }
            )

        # CRITICAL FIX: Handle attached files for RUNNING sessions
        # Our initial P0 fix only handled 'created' status, but if auto_start=true
        # was used (incorrectly), the session is already 'running' when we try to
        # send files. We need to handle this case too.
        # Note: Only process if message wasn't already queued above
        if request.attached_files and status == "running" and not message_queued:
            logger.info(f"📁 Processing {len(request.attached_files)} attached file(s) for RUNNING session")

            # STEP 1: Verify all attached files are ready
            files_ready, missing_files = await verify_files_ready(
                request.attached_files,
                session_id,
                user_id,
                max_wait_seconds=10.0
            )

            if not files_ready:
                logger.error(f"📁 File verification failed for running session. Missing/unreadable: {missing_files}")
                raise HTTPException(
                    status_code=425,  # Too Early
                    detail=f"Files not ready: {', '.join(missing_files)}. Please retry in a few seconds.",
                    headers={"Retry-After": "5"}
                )

            logger.info(f"📁 ✅ All {len(request.attached_files)} file(s) verified and ready for running session")

            # STEP 2: Process files to create metadata
            image_attachments_running = []
            message_text_running = request.text

            for file_path in request.attached_files:
                message_text_running, images = await inject_file_content_to_message(
                    file_path,
                    message_text_running,
                    session_id,
                    user_id
                )

                if images:
                    image_attachments_running.extend(images)
                    logger.info(f"📷 Collected {len(images)} image(s) for vision from {file_path} (running session)")

            # STEP 3: Add images to metadata and queue message
            metadata_running = request.metadata or {}
            if image_attachments_running:
                metadata_running['image_attachments'] = image_attachments_running
                logger.info(f"📷 Prepared message with {len(image_attachments_running)} image(s) for running session")

            # STEP 4: Queue the message (agent will process on next queue check)
            # Note: We already queued the message earlier at line 234-239, but that was
            # BEFORE file processing. We need to queue again with the processed metadata.
            # Actually, wait - looking at the code flow, we haven't queued yet for running
            # sessions. The queue happens at line 234-239 but that's inside the "created"
            # branch. So we need to queue here.
            await orchestrator.submit_user_message(
                None,  # Let orchestrator route to active agent
                message_text_running,
                request.kind,
                metadata_running
            )
            message_queued = True  # Mark as queued to prevent any further queueing

            logger.info(f"✅ Queued message with {len(image_attachments_running)} image(s) for running session {session_id}")

            # Emit to feed
            if hasattr(agent, 'session_manager'):
                try:
                    agent.session_manager.add_to_feed(
                        session_id,
                        'user_message',
                        {
                            'text': request.text,
                            'kind': request.kind,
                            'metadata': request.metadata or {},
                            'timestamp': time.time()
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to add user message to feed: {e}")

            return MessageResponse(
                success=True,
                message=f"Message with {len(image_attachments_running)} image(s) queued for running session",
                metadata={
                    "session_id": session_id,
                    "resumed": False,
                    "images_attached": len(image_attachments_running)
                }
            )

        # For non-completed sessions WITHOUT attached files
        return MessageResponse(
            success=True,
            message="Message sent successfully",
            metadata={"session_id": session_id, "resumed": False}
        )

    except HTTPException:
        raise
    except Exception as e:
        # Import here to avoid circular imports
        from core.exceptions import MessageQueueFullError
        
        # Handle specific exception types with proper HTTP status codes
        if isinstance(e, MessageQueueFullError):
            logger.warning(f"Message queue full for session {session_id}: {e}")
            raise HTTPException(
                status_code=429,
                detail=str(e),
                headers={"Retry-After": "30"}
            )
        elif isinstance(e, ValueError):
            error_str = str(e)
            # Handle rate limit errors from HITL manager
            if "Rate limit exceeded" in error_str:
                logger.warning(f"Rate limit exceeded for session {session_id}")
                raise HTTPException(
                    status_code=429,
                    detail=error_str,
                    headers={"Retry-After": "60"}
                )
            raise HTTPException(status_code=400, detail=error_str)
        else:
            logger.error(f"Error sending user message: {e}")
            raise HTTPException(status_code=500, detail=str(e))

@router.post("/sessions/{session_id}/cancel", response_model=MessageResponse)
async def cancel_session(
    session_id: str,
    req: Request,
    agent = Depends(get_task_agent)
):
    """Cancel a running session"""
    try:
        # OPTIMIZATION: Clean session ID once at API entry
        session_id = clean_session_id_at_entry(session_id)

        session_info = await agent.get_session_by_id(session_id)
        if not session_info:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

        # IDOR: the caller must own this session. cancel_session_by_id self-authorizes
        # against the session's OWN user_id, so without this gate any authenticated
        # tenant could force-cancel another tenant's in-flight (paid) run.
        _require_session_owner(req, session_info.get('user_id'))

        # Call agent's cancel method
        success = await agent.cancel_session_by_id(session_id, force=True)
        
        if success:
            return MessageResponse(
                success=True,
                message="Session cancelled successfully"
            )
        else:
            raise HTTPException(status_code=400, detail="Failed to cancel session")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling session: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/sessions/{session_id}", response_model=SessionStatusResponse)
async def get_session_status(
    session_id: str,
    req: Request,
    agent = Depends(get_task_agent)
):
    """Get the status of a session with user-friendly status"""
    try:
        # OPTIMIZATION: Clean session ID once at API entry
        session_id = clean_session_id_at_entry(session_id)

        session_info = await agent.get_session_by_id(session_id)
        if not session_info:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

        # E8 (A6 gap 4): the caller must own this session — any authenticated
        # caller could otherwise read another tenant's session (task, config, metadata).
        _require_session_owner(req, session_info.get('user_id'))

        # Get internal status
        internal_status = normalize_status_value(session_info.get("status", "unknown"))
        
        # Convert to user-facing status
        from agents.task.agent.session import get_user_status
        user_status = get_user_status(internal_status)
        
        # Determine user capabilities
        can_cancel = (user_status == "active")
        can_message = True  # Always allow messages
        
        # Build response with user-facing status
        response_data = {
            "id": session_info.get("id", session_id),
            "session_id": session_info.get("id", session_id),
            "user_id": session_info.get("user_id", "_anonymous_"),
            "task": session_info.get("task", "Unknown task"),
            "status": user_status,  # User-facing: active/idle/stopped
            "can_cancel": can_cancel,
            "can_send_message": can_message,
            "created_at": session_info.get("created_at", datetime.now().isoformat()),
            "last_updated": session_info.get("last_updated"),
            "internal_status": internal_status,  # For debugging
            "agents": {},  # Simplified for user
            "config": session_info.get("config", {}),
            "metadata": session_info.get("metadata", {}),
        }
        
        # Extract model and tools from config if present
        config = session_info.get("config", {})
        response_data["model"] = config.get("model")
        response_data["tools"] = config.get("tools", [])
        
        # Add webview URL if present in metadata
        metadata = session_info.get("metadata", {})
        response_data["webview_url"] = metadata.get("webview_url")

        # 019 P1: live run activity (in-process snapshot; None when this worker
        # doesn't own the session or nothing has run since restart).
        try:
            from agents.task.telemetry.run_activity import get_activity
            response_data["current_activity"] = get_activity(session_id)
        except Exception:
            response_data["current_activity"] = None

        return SessionStatusResponse(**response_data)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/sessions/{session_id}/queue-status", response_model=dict)
async def get_queue_status(
    session_id: str,
    req: Request,
    agent = Depends(get_task_agent)
):
    """Get message queue status for a session.

    Returns queue status for active sessions, or final status for completed/failed sessions.
    This prevents 404 errors when WebView polls completed sessions.

    Args:
        session_id: Session identifier
        agent: TaskAgent dependency injection

    Returns:
        {
            "queued_messages": int,
            "agent_status": str,
            "session_completed": bool,
            "streaming_callbacks": int,
            "callback_failures": int
        }

    Raises:
        HTTPException 404: Session not found
    """
    try:
        # Clean session ID
        session_id = clean_session_id_at_entry(session_id)

        # Item 6: honest 409 (not false-404) if owned by another worker. No-op locally.
        guard_remote(agent, session_id)

        # IDOR: fetch metadata and confirm the caller owns this session BEFORE exposing
        # runtime state (agent_status/queued/callbacks) or using 404-vs-200 as a
        # session-existence enumeration oracle.
        session_info = await agent.get_session_by_id(session_id)
        if not session_info:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        _require_session_owner(req, session_info.get('user_id'))

        # Try to get orchestrator from active sessions
        orchestrator = agent.get_orchestrator(session_id)

        if not orchestrator:
            # ✅ FIX #4: session exists but is not active - return final status
            session_status = normalize_status_value(session_info.get('status', 'unknown'))

            if session_status in ['completed', 'failed', 'suspended', 'error']:
                logger.debug(f"Session {session_id} is {session_status}, returning final status")
                return {
                    "queued_messages": 0,
                    "agent_status": session_status,
                    "session_completed": True,
                    "streaming_callbacks": 0,
                    "callback_failures": 0,
                    "message": f"Session {session_status}"
                }

            # Session exists but has no active orchestrator and a non-final status
            raise HTTPException(
                status_code=404,
                detail=f"Session {session_id} not found"
            )

        # Session is active - return current status
        agents = orchestrator.agents
        if not agents:
            logger.warning(f"Session {session_id} orchestrator has no agents")
            return {
                "queued_messages": 0,
                "agent_status": "no_agent",
                "session_completed": False,
                "streaming_callbacks": 0,
                "callback_failures": 0
            }

        # Get first agent (usually there's only one)
        first_agent = next(iter(agents.values()))

        # Check if agent has HITL manager
        if not hasattr(first_agent, 'hitl_manager'):
            logger.warning(f"Agent {first_agent.agent_id} has no hitl_manager")
            return {
                "queued_messages": 0,
                "agent_status": "no_hitl",
                "session_completed": False,
                "streaming_callbacks": 0,
                "callback_failures": 0
            }

        # Get stats from HITL manager
        stats = first_agent.hitl_manager.get_stats()

        # Determine agent status
        agent_status = "running"
        if hasattr(first_agent, 'is_done') and first_agent.is_done:
            agent_status = "done"
        elif hasattr(first_agent, 'is_running') and not first_agent.is_running:
            agent_status = "idle"

        return {
            "queued_messages": stats.get("queued_messages", 0),
            "agent_status": agent_status,
            "session_completed": False,
            "streaming_callbacks": stats.get("streaming_callbacks", 0),
            "callback_failures": stats.get("callback_failures", 0)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting queue status: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal error: {str(e)}"
        )

@router.get("/users/{user_id}/sessions", response_model=Dict[str, Any])
async def get_user_sessions(
    user_id: str,
    req: Request,
    agent = Depends(get_task_agent)
):
    """Get all sessions for a user"""
    try:
        # E8 (A6 gap 4): the caller must be the user whose sessions are requested —
        # any authenticated caller could otherwise list another tenant's sessions.
        _require_session_owner(req, user_id)

        # Get all sessions for user from agent's active sessions (nested structure)
        user_sessions = []
        if hasattr(agent, 'active_sessions') and user_id in agent.active_sessions:
            for sid, session_data in agent.active_sessions[user_id].items():
                session_info = await agent.get_session_by_id(sid)
                if session_info:
                    user_sessions.append(session_info)
        
        # Get active session from agent state
        active_session_id = None
        if hasattr(agent, 'user_sessions') and user_id in agent.user_sessions:
            active_session_id = agent.user_sessions.get(user_id)
        
        return {
            "ok": True,
            "sessions": user_sessions,
            "active_session_id": active_session_id
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user sessions: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/users/{user_id}/active_session", response_model=MessageResponse)
async def switch_active_session(
    user_id: str,
    request: Dict[str, str],
    req: Request,
    agent = Depends(get_task_agent)
):
    """Switch the active session for a user"""
    try:
        # E8 (A6 gap 4): the caller must be the user whose active-session pointer
        # is being changed — any authenticated caller could otherwise hijack it.
        _require_session_owner(req, user_id)

        session_id = request.get("session_id")
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")

        # Set active session through agent
        if hasattr(agent, 'user_sessions'):
            agent.user_sessions[user_id] = session_id
            success = True
        else:
            success = False

        if success:
            return MessageResponse(
                success=True,
                message=f"Switched to session {session_id}",
                metadata={"session_id": session_id}
            )
        else:
            raise HTTPException(status_code=400, detail="Failed to switch session")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error switching session: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _capability_defaults(config) -> tuple:
    """Default (provider, model) advertised by /capabilities.

    Falls back to the provider's policy default (llm_client_registry.DEFAULT_MODELS,
    env-overridable via POLYROB_<PROVIDER>_MODEL) — never a hardcoded literal, which
    is how the deprecated x-ai/grok-4.1-fast kept being advertised after the registry
    had already marked it dead (structural audit T2, 2026-07-16).
    """
    from modules.llm.llm_client_registry import get_default_model
    provider = getattr(config, 'provider', None) or 'openrouter'
    model = getattr(config, 'model', None) or get_default_model(provider)
    return provider, model


@router.get("/capabilities")
async def get_capabilities(request: Request):
    """Get system capabilities including available models and tools."""
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()
        
        # FIXED: LLMManager is registered as 'llm', not 'llm_manager'
        llm_manager = container.get_service("llm")
        
        # Get available models (only from initialized providers)
        # Include pricing and context info from registry
        from modules.llm.model_registry import get_model_config

        available_models = []
        if llm_manager and hasattr(llm_manager, 'get_available_models'):
            model_tuples = await llm_manager.get_available_models()
            for provider, model in model_tuples:
                model_info = {"provider": provider, "model": model}
                # Enrich with registry data
                config = get_model_config(model)
                if config:
                    model_info["context_window"] = config.context_window
                    model_info["max_output"] = config.max_completion_tokens
                    if config.pricing:
                        model_info["price_input"] = config.pricing.input_price
                        model_info["price_output"] = config.pricing.output_price
                available_models.append(model_info)
        else:
            # P0.6: fallback (no llm_manager) now rides the ONE catalog too. Import the
            # builder under an alias to avoid shadowing the local `available_models` list.
            from modules.llm.available_models import available_models as _build_models
            for c in _build_models():
                model_info = {"provider": c.provider, "model": c.model}
                config = get_model_config(c.model)
                if config:
                    model_info["context_window"] = config.context_window
                    model_info["max_output"] = config.max_completion_tokens
                    if config.pricing:
                        model_info["price_input"] = config.pricing.input_price
                        model_info["price_output"] = config.pricing.output_price
                available_models.append(model_info)
        
        # Get available tools dynamically from descriptors
        # Uses centralized functions to ensure consistency across codebase
        from tools.descriptors import get_agent_usable_tools, get_tool_display_name, get_default_tools

        agent_tools = get_agent_usable_tools()
        available_tools = {}

        for tool_name, descriptor in agent_tools.items():
            # Get display name (e.g., browser_manager -> browser)
            display_name = get_tool_display_name(tool_name)

            # Check if tool is initialized in container
            tool = container.get_service(tool_name)
            initialized = tool is not None

            # Special case: browser_manager is registered as 'browser_manager' but displayed as 'browser'
            if tool_name == 'browser_manager' and not initialized:
                # Check if already checked
                pass  # Already using tool_name for lookup

            available_tools[display_name] = {
                "name": display_name,
                "initialized": initialized,
                "description": descriptor.description,
                "category": descriptor.category.value if descriptor.category else "other"
            }

        # Get default tools from centralized source
        default_tools_list = get_default_tools()
        
        # Get default configuration
        config = container.config
        default_provider, default_model = _capability_defaults(config)

        # Get MCP server information if available, including connection status
        mcp_servers = {"global": [], "user": [], "include_global": True}
        mcp_tool = container.get_service('mcp')
        if mcp_tool and hasattr(mcp_tool, 'server_manager') and mcp_tool.server_manager:
            try:
                # Get detailed server info including status
                servers_info = await mcp_tool.server_manager.list_servers()
                for server_info in servers_info:
                    name = server_info.get('name', '')
                    status = server_info.get('status', 'unknown')
                    last_error = server_info.get('last_error', None)
                    tools_count = server_info.get('tools_count', 0)

                    server_entry = {
                        "name": name,
                        "tool_id": f"mcp:{name}",
                        "type": "global",
                        "status": status,  # connected, error, disconnected
                        "tools_count": tools_count
                    }
                    # Include error message if server failed to connect
                    if status == "error" and last_error:
                        server_entry["error"] = last_error

                    mcp_servers["global"].append(server_entry)
            except Exception as e:
                logger.warning(f"Failed to get MCP server details: {e}")
                # Fallback to basic listing
                if hasattr(mcp_tool, 'get_global_server_names'):
                    global_server_names = mcp_tool.get_global_server_names()
                    for full_name in global_server_names:
                        name = full_name.replace("global::", "")
                        mcp_servers["global"].append({
                            "name": name,
                            "tool_id": f"mcp:{name}",
                            "type": "global",
                            "status": "unknown"
                        })

        # Get user MCP servers if authenticated
        user_id = getattr(request.state, 'user_id', None)
        if user_id:
            user_mcp_service = container.get_service('user_mcp_service')
            if user_mcp_service:
                try:
                    result = await user_mcp_service.get_available_servers_for_session(user_id)
                    mcp_servers["user"] = result.get("user", [])
                    mcp_servers["include_global"] = result.get("include_global", True)
                except Exception as e:
                    logger.warning(f"Failed to get user MCP servers: {e}")

        return {
            "models": available_models,
            "tools": available_tools,
            "mcp_servers": mcp_servers,
            "default_model": default_model,
            "default_provider": default_provider,
            "default_tools": default_tools_list  # From descriptors.get_default_tools()
        }
        
    except Exception as e:
        logger.error(f"Error getting capabilities: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/sessions")
async def create_session(
    request_body: Dict[str, Any],
    req: Request,
    agent = Depends(get_task_agent)
):
    """Create and start a new Task session.

    BETA: Requires DEN token ownership for session creation.
    Supports both credits (for authenticated users) and x402 payment.
    """
    try:
        from agents.task_agent_lite import SessionRequest
        from api.payment_verification import verify_payment_for_request, payment_required_response
        from fastapi.responses import JSONResponse
        from core.exceptions import SessionOwnershipError

        # Extract parameters with defaults
        task = request_body.get("task")
        if not task:
            raise HTTPException(status_code=400, detail="Task is required")

        # PAYMENT VERIFICATION (NEW)
        # This fixes the critical gap: endpoints now verify payment before execution
        try:
            payment_method, payment_details = await verify_payment_for_request(
                request=req,
                cost_credits=1  # Session creation = 1 credit ($0.01) - LLM costs metered separately
            )
            logger.info(f"Payment verified via {payment_method}: {payment_details}")
        except HTTPException as e:
            if e.status_code == 402:
                # Return payment options (credits + x402)
                response_data = payment_required_response(req, cost_credits=1)
                return JSONResponse(status_code=402, content=response_data)
            raise

        # SECURITY: Get user_id from authenticated JWT token or x402 payment
        # Request body user_id is ignored to prevent session hijacking
        from utils.auth_utils import get_authenticated_user_id, get_user_tier
        import os

        # If paid via x402, get user_id from request state (set by middleware)
        if payment_method == "x402":
            # Middleware sets proper user_id (usr_xxx format) from wallet
            user_id = getattr(req.state, 'user_id', None)
            if not user_id:
                # Fallback: generate from payer_address
                from core.identity import generate_user_id_from_wallet
                payer_address = payment_details.get("payer_address")
                if payer_address:
                    user_id = generate_user_id_from_wallet(payer_address)
                else:
                    user_id = "x402_user"
            tier = "x402"  # x402 users have special tier
        else:
            user_id = get_authenticated_user_id(req)
            tier = get_user_tier(req)

        logger.info(f"Creating session for user: {user_id} (tier: {tier}, payment: {payment_method})")

        # BETA ACCESS: Only allow tiers with full access
        # - free: BLOCKED (no DEN token, no admin grant)
        # - free_access: ALLOWED (admin-granted access, uses credits)
        # - holder: ALLOWED (has DEN token)
        # - x402: ALLOWED (pay-per-request)
        # - admin: ALLOWED (via admin_bypass payment method)
        from api.auth_constants import has_full_access
        is_development = os.getenv('ENVIRONMENT', 'production') == 'development'
        is_admin_user = payment_method == "admin_bypass"

        if not is_development and not is_admin_user and not has_full_access(tier):
            raise HTTPException(
                status_code=403,
                detail="Beta access: Task automation requires DEN token ownership or admin-granted access. Join https://t.me/tmachinrobot for access."
            )
        
        # Build session config with tools_config if provided
        from agents.task.config import TaskSessionConfig
        from agents.task.utils import detect_llm_provider

        session_config = TaskSessionConfig.defaults()
        session_config.llm.model = request_body.get("model", "gpt-5")

        # Auto-detect provider from model name if not specified
        if "provider" in request_body:
            session_config.llm.provider = request_body["provider"]
        else:
            session_config.llm.provider = detect_llm_provider(None, session_config.llm.model)
            logger.info(f"Auto-detected provider '{session_config.llm.provider}' for model '{session_config.llm.model}'")

        session_config.llm.temperature = request_body.get("temperature", 0.0)

        # Auto-detect vision support from model registry if not explicitly set
        if "use_vision" in request_body:
            session_config.llm.use_vision = request_body["use_vision"]
        else:
            # Check model registry for vision support
            from modules.llm.model_registry import get_model_config
            model_config = get_model_config(session_config.llm.model)
            if model_config and model_config.capabilities:
                session_config.llm.use_vision = model_config.capabilities.supports_vision
                logger.info(f"Auto-detected vision support for '{session_config.llm.model}': {session_config.llm.use_vision}")
            else:
                session_config.llm.use_vision = False  # Conservative default for unknown models
                logger.warning(f"Model '{session_config.llm.model}' not in registry - defaulting to no vision")

        session_config.limits.max_steps = request_body.get("max_steps", 50)
        session_config.tools = request_body.get("tools", ["browser", "filesystem"])

        # Handle MCP server selection
        # mcp_servers is a list of tool_ids like ["mcp:anysite", "mcp:user:myserver"]
        mcp_servers = request_body.get("mcp_servers", [])
        if mcp_servers:
            # Add 'mcp' to tools if not present and we have MCP servers selected
            if "mcp" not in session_config.tools:
                session_config.tools.append("mcp")
            # Store requested MCP servers in tools_config for the orchestrator
            if not hasattr(session_config, 'tools_config') or session_config.tools_config is None:
                session_config.tools_config = {}
            session_config.tools_config["mcp_servers"] = mcp_servers
            logger.info(f"Session configured with MCP servers: {mcp_servers}")

        # Add tools_config if provided
        if "tools_config" in request_body:
            if not hasattr(session_config, 'tools_config') or session_config.tools_config is None:
                session_config.tools_config = {}
            session_config.tools_config.update(request_body.get("tools_config", {}))

        # Create session request with session_config
        session_request = SessionRequest(
            task=task,
            model=session_config.llm.model,
            provider=session_config.llm.provider,
            tools=session_config.tools,
            max_steps=session_config.limits.max_steps,
            temperature=session_config.llm.temperature,
            use_vision=session_config.llm.use_vision,
            session_config=session_config.to_dict()
        )

        # user_id already extracted above (line 732)
        session_id = request_body.get("session_id")  # Allow CLI to specify
        
        # Create session
        try:
            session_info = await agent.create_session(
                user_id,
                session_request,
                session_id=session_id
            )
        except SessionOwnershipError as owner_error:
            # C4: client-supplied session_id belongs to another user.
            raise HTTPException(status_code=403, detail=str(owner_error))
        except Exception as create_error:
            logger.error(f"Session creation failed: {create_error}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to create session: {str(create_error)}")

        if not session_info:
            raise HTTPException(status_code=500, detail="Failed to create session - no session info returned")

        session_id = session_info.get("id")

        # Create workspace directory immediately to prevent race condition with file uploads
        from agents.task.path import pm
        workspace_dir = pm().get_workspace_dir(session_id, user_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created workspace directory for session {session_id}: {workspace_dir}")

        # Generate WebView URL using the utility function (single source of truth)
        from agents.task.utils_webview import get_webview_url
        webview_url = get_webview_url(session_id)

        # Store in session metadata
        if hasattr(agent, 'session_manager'):
            try:
                agent.session_manager.update_session_metadata(
                    session_id, {"webview_url": webview_url}
                )
            except Exception as e:
                logger.debug(f"Failed to store webview URL in metadata: {e}")

        # AUTO-START: Check if client wants to delay start (for file uploads)
        # CRITICAL: Log the received value for debugging
        auto_start_requested = request_body.get("auto_start", True)
        logger.info(f"📋 Session creation auto_start parameter: {auto_start_requested} (type: {type(auto_start_requested).__name__})")

        # CRITICAL FIX: Force auto_start=false if files will be attached
        # Don't trust frontend - enforce on backend to prevent race conditions
        wait_for_uploads = request_body.get("wait_for_uploads", False)
        has_attached_files = request_body.get("attached_files") and len(request_body.get("attached_files", [])) > 0

        if wait_for_uploads or has_attached_files:
            auto_start = False
            logger.info(f"🔒 FORCING auto_start=false (wait_for_uploads={wait_for_uploads}, has_attached_files={has_attached_files})")
        else:
            auto_start = auto_start_requested
            logger.info(f"✓ Using requested auto_start={auto_start}")

        if auto_start:
            # Start agent execution in background with the task
            logger.info(f"Auto-starting session {session_id} with task: {task[:100]}...")

            import asyncio

            async def start_session_with_task():
                try:
                    logger.info(f"Starting session execution for {session_id}")
                    result = await agent.run_session(user_id, session_id)
                    logger.info(f"Session {session_id} completed with result: {result}")
                except Exception as e:
                    logger.error(f"Session {session_id} failed with error: {e}", exc_info=True)

            _spawn_session_task(start_session_with_task())

            # Return immediately - agent runs in background
            return SessionResponse(
                ok=True,
                session_id=session_id,
                task=task,
                status="running",
                model=session_request.model,
                tools=session_request.tools,
                webview_url=webview_url,
                message="Session created and started successfully"
            )
        else:
            # Client will upload files and send first message manually
            logger.info(f"Created session {session_id} for user {user_id} (waiting for first message)")
            return SessionResponse(
                ok=True,
                session_id=session_id,
                task=task,
                status="created",
                model=session_request.model,
                tools=session_request.tools,
                webview_url=webview_url,
                message="Session created - send a message to start"
            )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating session: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/metrics", response_model=dict)
async def get_resource_metrics(agent = Depends(get_task_agent)):
    """Get current resource usage metrics.

    Returns:
        Resource usage statistics for monitoring
    """
    try:
        import psutil
        import os

        # Get process memory
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()

        # Session metrics
        sessions_in_memory = agent.active_session_count()
        max_sessions = agent.max_sessions_in_memory

        # Browser metrics
        browser_manager = None
        if agent.container and agent.container.has_service('browser_manager'):
            browser_manager = agent.container.get_service('browser_manager')

        browser_metrics = {}
        if browser_manager:
            browser_metrics = {
                "contexts_in_use": len(browser_manager.contexts_in_use),
                "max_contexts": browser_manager.browser_config.max_contexts,
                "contexts_in_pool": len(browser_manager.context_pool),
                "wait_queue_depth": len(browser_manager._wait_queue),
                "utilization_pct": (len(browser_manager.contexts_in_use) / browser_manager.browser_config.max_contexts * 100) if browser_manager.browser_config.max_contexts > 0 else 0
            }

        # Session breakdown by status
        all_sessions = agent.get_all_sessions()
        status_counts = {}
        for session_id, session_info in all_sessions.items():
            status = session_info.get('status', 'unknown')
            status_counts[status] = status_counts.get(status, 0) + 1

        # User distribution
        user_session_counts = {}
        for session_id, session_info in all_sessions.items():
            user_id = session_info.get('user_id', '_anonymous_')
            user_session_counts[user_id] = user_session_counts.get(user_id, 0) + 1

        # Message queue metrics
        total_queued_messages = 0
        for orchestrator in agent.active_orchestrators():
            for agent_obj in orchestrator.agents.values():
                if hasattr(agent_obj, 'hitl_manager') and agent_obj.hitl_manager:
                    total_queued_messages += agent_obj.hitl_manager.get_queue_size()

        return {
            "timestamp": datetime.now().isoformat(),
            "sessions": {
                "in_memory": sessions_in_memory,
                "max_allowed": max_sessions,
                "utilization_pct": (sessions_in_memory / max_sessions * 100) if max_sessions > 0 else 0,
                "by_status": status_counts,
                "total_across_all_users": len(all_sessions)
            },
            "browser": browser_metrics,
            "memory": {
                "process_mb": memory_info.rss / 1024 / 1024,
                "process_mb_peak": memory_info.vms / 1024 / 1024,
                "system_available_mb": psutil.virtual_memory().available / 1024 / 1024,
                "system_total_mb": psutil.virtual_memory().total / 1024 / 1024,
                "system_used_pct": psutil.virtual_memory().percent
            },
            "messages": {
                "total_queued": total_queued_messages,
                "queues_active": sum(1 for o in agent.active_orchestrators() for a in o.agents.values() if hasattr(a, 'hitl_manager'))
            },
            "users": {
                "total_users": len(user_session_counts),
                "sessions_per_user": user_session_counts,
                "top_users": sorted(user_session_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            }
        }

    except Exception as e:
        logger.error(f"Error getting metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def verify_files_ready(
    file_paths: List[str],
    session_id: str,
    user_id: str,
    max_wait_seconds: float = 10.0
) -> tuple[bool, List[str]]:
    """Verify that all uploaded files exist and are readable.

    Implements explicit synchronization instead of relying on timing.
    Retries with exponential backoff to handle filesystem latency.

    Args:
        file_paths: List of relative file paths (e.g., ['image.png'])
        session_id: Session ID for workspace resolution
        user_id: User ID for workspace resolution
        max_wait_seconds: Maximum time to wait for files (default 10s)

    Returns:
        tuple: (all_ready: bool, missing_files: List[str])
    """
    from pathlib import Path
    from agents.task.path import pm
    import asyncio

    workspace_dir = pm().get_workspace_dir(session_id, user_id)

    # Exponential backoff: 0.1s, 0.2s, 0.4s, 0.8s, 1.6s, 3.2s, ...
    wait_time = 0.1
    total_waited = 0.0
    attempt = 0

    while total_waited < max_wait_seconds:
        attempt += 1
        missing_files = []
        unreadable_files = []

        for file_path in file_paths:
            full_path = workspace_dir / file_path

            # Check existence
            if not full_path.exists():
                missing_files.append(file_path)
                continue

            # Check readability and size
            try:
                stat = full_path.stat()
                if stat.st_size == 0:
                    unreadable_files.append(f"{file_path} (0 bytes)")
                    continue

                # Try to open for reading (verifies no write lock)
                with open(full_path, 'rb') as f:
                    # Read first byte to ensure file is actually accessible
                    f.read(1)

            except (IOError, PermissionError) as e:
                unreadable_files.append(f"{file_path} ({e})")

        # All files ready?
        if not missing_files and not unreadable_files:
            if attempt > 1:
                logger.info(
                    f"📁 All {len(file_paths)} file(s) verified ready after {attempt} attempts "
                    f"({total_waited:.2f}s total wait)"
                )
            return (True, [])

        # Log status on first attempt and every 5th attempt
        if attempt == 1 or attempt % 5 == 0:
            logger.info(
                f"📁 Waiting for files (attempt {attempt}, {total_waited:.2f}s waited): "
                f"missing={len(missing_files)}, unreadable={len(unreadable_files)}"
            )
            if missing_files:
                logger.debug(f"   Missing: {missing_files}")
            if unreadable_files:
                logger.debug(f"   Unreadable: {unreadable_files}")

        # Wait before retry
        await asyncio.sleep(wait_time)
        total_waited += wait_time
        wait_time = min(wait_time * 2, 2.0)  # Cap at 2s

    # Timeout - return what's missing
    all_problems = missing_files + unreadable_files
    logger.warning(
        f"📁 File verification timeout after {total_waited:.2f}s. "
        f"Problems with {len(all_problems)} file(s): {all_problems}"
    )
    return (False, all_problems)

async def inject_file_content_to_message(
    file_path,
    message_text: str,
    session_id: str,
    user_id: str
) -> tuple[str, Optional[List[Dict[str, Any]]]]:
    """Smart content injection with IMAGE support for vision.

    Returns both updated text and optional image attachments for vision models.

    Small text files (< 30KB): Inject full content inline
    Large text files (>= 30KB): Just mention with metadata
    Images: Convert to base64 for vision models

    Args:
        file_path: Path to file in workspace
        message_text: User's message text
        session_id: Session ID for path resolution
        user_id: User ID for path resolution

    Returns:
        tuple: (combined_message_text, image_attachments)
        - combined_message_text: Text with file references
        - image_attachments: List of image data dicts for vision (None if no images)
    """
    from pathlib import Path
    from agents.task.path import pm
    import aiofiles
    import base64

    # Get full file path
    workspace_dir = pm().get_workspace_dir(session_id, user_id)
    full_path = workspace_dir / file_path

    if not full_path.exists():
        return (f"{message_text}\n\n[Error: Attached file not found: {file_path}]", None)

    file_size = full_path.stat().st_size
    file_ext = full_path.suffix.lower()

    # IMAGE HANDLING (NEW): Convert to base64 for vision
    IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    if file_ext in IMAGE_EXTENSIONS:
        try:
            # Validate image size (10MB max, same as upload limit)
            MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
            if file_size > MAX_IMAGE_SIZE:
                size_mb = file_size / 1024 / 1024
                return (
                    f"{message_text}\n\n[Error: Image too large ({size_mb:.1f}MB). Max: 10MB for vision processing]",
                    None
                )

            # Read image as binary and convert to base64
            # Using synchronous I/O (FastAPI handles blocking ops via thread pool)
            with open(full_path, 'rb') as f:
                image_bytes = f.read()

            image_base64 = base64.b64encode(image_bytes).decode('utf-8')

            # Determine MIME type
            mime_map = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif',
                '.webp': 'image/webp'
            }
            mime_type = mime_map.get(file_ext, 'image/png')

            # Return text + image data for multimodal message
            size_kb = file_size / 1024
            size_str = f"{size_kb:.1f}KB" if size_kb < 1024 else f"{size_kb/1024:.1f}MB"

            updated_text = f"{message_text}\n\n[Attached image: {full_path.name} ({size_str})]\nPath in workspace: {file_path}"
            image_data = {
                'type': 'image_url',
                'image_url': {
                    'url': f'data:{mime_type};base64,{image_base64}'
                }
            }

            logger.info(f"📷 Image attached for vision: {full_path.name} ({size_str})")
            return (updated_text, [image_data])

        except Exception as e:
            logger.error(f"Failed to process image {full_path.name}: {e}")
            return (
                f"{message_text}\n\n[Error: Failed to process image {full_path.name}: {str(e)}]",
                None
            )

    # TEXT FILE HANDLING (existing logic)
    INJECT_SIZE_THRESHOLD = 30 * 1024  # 30KB

    if file_size < INJECT_SIZE_THRESHOLD:
        # Small file - inject full content
        if file_ext in ['.txt', '.md', '.csv', '.json', '.xml', '.py', '.js', '.html', '.css']:
            try:
                # Using synchronous I/O (FastAPI handles blocking ops via thread pool)
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                combined_text = f"""{message_text}

[Attached file: {full_path.name} ({file_size/1024:.1f}KB)]
--- File Content Start ---
{content}
--- File Content End ---"""
                return (combined_text, None)
            except UnicodeDecodeError:
                # Fall through to large file handling
                pass

    # Large file or binary - just reference
    size_kb = file_size / 1024
    size_str = f"{size_kb:.1f}KB" if size_kb < 1024 else f"{size_kb/1024:.1f}MB"

    reference_text = f"""{message_text}

[Attached large file: {full_path.name} ({size_str})]
File type: {file_ext}
Path in workspace: {file_path}

To read this file, use: filesystem_read_file(path="{file_path}")"""

    return (reference_text, None)

@router.post("/sessions/{session_id}/workspace/upload")
async def upload_document(
    session_id: str,
    file: UploadFile = File(...),
    req: Request = None,
    agent = Depends(get_task_agent)
):
    """Upload a file to session workspace.

    Supports: 
    - Documents: PDF, DOCX, DOC, TXT, MD, CSV, JSON, XML
    - Images: PNG, JPG, JPEG, GIF, WEBP (for vision)
    
    Max size: 10MB per file
    """
    from pathlib import Path
    from agents.task.path import pm
    import aiofiles

    try:
        # 1. Clean session ID
        session_id = clean_session_id_at_entry(session_id)

        # 2. Validate session exists
        session_info = agent.session_manager.get_session_info(session_id)
        if not session_info:
            raise HTTPException(status_code=404, detail="Session not found")

        # 3. Security: Get authenticated user_id
        from utils.auth_utils import get_authenticated_user_id
        user_id = get_authenticated_user_id(req)

        # Verify user owns this session
        session_owner = session_info.get('user_id')
        logger.info(f"[Upload Auth Check] Token user_id: {user_id}, Session owner: {session_owner}, Match: {user_id == session_owner}")

        if session_owner != user_id:
            logger.warning(f"[Upload Auth FAILED] Access denied - user {user_id} tried to upload to session owned by {session_owner}")
            raise HTTPException(status_code=403, detail=f"Access denied - session owned by different user")

        # 4. Validate file type (documents + images for vision)
        ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.doc', '.txt', '.md', '.csv', '.json', '.xml', '.png', '.jpg', '.jpeg', '.gif', '.webp'}
        file_ext = Path(file.filename).suffix.lower()

        if file_ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"File type {file_ext} not allowed. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
            )

        # 5. Validate file size (10MB limit)
        MAX_SIZE = 10 * 1024 * 1024  # 10MB
        file_content = await file.read()
        if len(file_content) > MAX_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File too large. Max size: {MAX_SIZE/1024/1024}MB"
            )

        # 6. Validate MIME type (security: prevent file type spoofing)
        ALLOWED_MIME_TYPES = {
            'application/pdf',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',  # .docx
            'application/msword',  # .doc
            'text/plain',
            'text/markdown',
            'text/csv',
            'application/json',
            'application/xml',
            'text/xml',
            # Image types for vision
            'image/png',
            'image/jpeg',
            'image/gif',
            'image/webp'
        }

        try:
            import magic
            # Detect actual MIME type from file content (not just extension)
            detected_mime = magic.from_buffer(file_content, mime=True)

            if detected_mime not in ALLOWED_MIME_TYPES:
                logger.warning(f"Rejected file with MIME type {detected_mime}: {file.filename}")
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid file type detected: {detected_mime}. Allowed types: PDF, DOCX, DOC, TXT, MD, CSV, JSON, XML, PNG, JPG, JPEG, GIF, WEBP"
                )

            logger.info(f"File MIME type validated: {detected_mime} for {file.filename}")
        except ImportError:
            logger.warning("python-magic not installed - skipping MIME type validation (security risk!)")
        except Exception as e:
            logger.warning(f"MIME type detection failed: {e}, using extension check only")

        # 7. Sanitize filename (prevent path traversal)
        logger.info(f"[Upload] Step 7: Sanitizing filename '{file.filename}'")
        from utils.path_validator import sanitize_filename
        safe_filename = sanitize_filename(file.filename)
        logger.info(f"[Upload] Sanitized filename: '{safe_filename}'")

        # 8. Get workspace directory
        logger.info(f"[Upload] Step 8: Getting workspace directory for session {session_id}, user {user_id}")
        workspace_dir = pm().get_workspace_dir(session_id, user_id)
        logger.info(f"[Upload] Workspace directory: {workspace_dir}")
        file_path = workspace_dir / safe_filename
        logger.info(f"[Upload] Full file path: {file_path}")

        # Handle duplicate filenames
        if file_path.exists():
            # Add timestamp suffix
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = file_path.stem
            suffix = file_path.suffix
            safe_filename = f"{stem}_{timestamp}{suffix}"
            file_path = workspace_dir / safe_filename

        # 9. Write file to workspace
        # Using synchronous I/O (FastAPI handles blocking ops via thread pool)
        # Previous async implementation was hanging indefinitely
        logger.info(f"[Upload] Step 9: Writing {len(file_content)} bytes to {file_path}")
        try:
            with open(file_path, 'wb') as f:
                f.write(file_content)
            logger.info(f"[Upload] File write completed successfully")
        except Exception as write_error:
            logger.error(f"[Upload] File write failed: {write_error}", exc_info=True)
            raise

        logger.info(f"Uploaded document to session {session_id}: {safe_filename}")

        # 10. Generate file ID for client tracking
        file_id = str(uuid.uuid4())

        # 11. Determine if file can be auto-injected (30KB threshold)
        INJECT_SIZE_THRESHOLD = 30 * 1024  # 30KB
        can_inject = len(file_content) < INJECT_SIZE_THRESHOLD

        # 12. Add to feed for UI notification
        logger.info(f"[Upload] Step 12: Adding to feed for session {session_id}")
        agent.session_manager.add_to_feed(
            session_id,
            'document_uploaded',
            {
                'file_id': file_id,
                'filename': safe_filename,
                'size': len(file_content),
                'path': str(file_path.relative_to(workspace_dir)),
                'can_inject': can_inject,
                'timestamp': time.time()
            }
        )
        logger.info(f"[Upload] Feed notification added successfully")

        # 13. Notify workspace context of upload (for agent awareness)
        logger.info(f"[Upload] Step 13: Notifying workspace context")
        try:
            from agents.task.workspace_context import get_workspace_context
            workspace_ctx = get_workspace_context()
            workspace_ctx.notify_upload(
                session_id=session_id,
                user_id=user_id,
                filename=safe_filename,
                size=len(file_content)
            )
            logger.debug(f"Notified workspace context of upload: {safe_filename}")
        except Exception as e:
            logger.warning(f"Failed to notify workspace context: {e}")

        # 14. Return success response with file metadata
        # Determine file type for appropriate message
        is_image = file_ext in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
        file_type = "Image" if is_image else "Document"

        response_data = {
            'success': True,
            'file_id': file_id,
            'filename': safe_filename,
            'path': str(file_path.relative_to(workspace_dir)),
            'size': len(file_content),
            'can_inject': can_inject,
            'is_image': is_image,  # NEW: Flag for frontend to handle images differently
            'message': f'{file_type} uploaded successfully'
        }
        logger.info(f"[Upload] Step 14: Returning success response for {safe_filename}")
        return response_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading document: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@router.get("/sessions/{session_id}/documents")
async def list_session_documents(
    session_id: str,
    req: Request = None,
    agent = Depends(get_task_agent)
):
    """List all documents in session workspace with metadata."""
    from pathlib import Path
    from agents.task.path import pm

    try:
        # 1. Clean session ID
        session_id = clean_session_id_at_entry(session_id)

        # 2. Validate session and access
        session_info = agent.session_manager.get_session_info(session_id)
        if not session_info:
            raise HTTPException(status_code=404, detail="Session not found")

        from utils.auth_utils import get_authenticated_user_id
        user_id = get_authenticated_user_id(req)

        if session_info.get('user_id') != user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # 3. Get workspace directory
        workspace_dir = pm().get_workspace_dir(session_id, user_id)

        # 4. Scan for documents
        documents = []
        DOCUMENT_EXTENSIONS = {'.pdf', '.docx', '.doc', '.txt', '.md', '.csv', '.json', '.xml'}

        for file_path in workspace_dir.rglob('*'):
            if file_path.is_file() and file_path.suffix.lower() in DOCUMENT_EXTENSIONS:
                stat = file_path.stat()
                documents.append({
                    'filename': file_path.name,
                    'path': str(file_path.relative_to(workspace_dir)),
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                    'extension': file_path.suffix.lower()
                })

        # Sort by modification time (newest first)
        documents.sort(key=lambda x: x['modified'], reverse=True)

        return {
            'success': True,
            'session_id': session_id,
            'documents': documents,
            'count': len(documents)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing documents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
