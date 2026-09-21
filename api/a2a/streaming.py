"""A2A Streaming - Server-Sent Events for real-time updates.

Implements SSE streaming for A2A protocol, enabling real-time
task status updates and artifact delivery.

Supports:
- message/stream: Initial streaming request
- tasks/resubscribe: Reconnection after connection loss

Reference: https://a2a-protocol.org/latest/topics/streaming-and-async/
"""

import asyncio
import json
import logging
from typing import AsyncGenerator, Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

from api.a2a.models import (
    A2ATask, A2ATaskState, A2ATaskStatus, A2AMessage,
    TaskStatusUpdateEvent, TaskArtifactUpdateEvent,
    JSONRPCResponse, SendMessageRequest, TaskResubscriptionRequest
)
from api.a2a.task_handler import A2ATaskHandler
from api.dependencies import get_user_permissive

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/a2a", tags=["a2a-streaming"])


async def task_event_stream(
    task_id: str,
    handler: A2ATaskHandler,
    history_length: Optional[int] = None
) -> AsyncGenerator[str, None]:
    """Generate SSE events for task status changes.

    Streams TaskStatusUpdateEvent and TaskArtifactUpdateEvent
    until task reaches terminal state.

    Args:
        task_id: Task/session ID to monitor
        handler: A2A task handler
        history_length: Optional number of events to replay

    Yields:
        SSE formatted event strings
    """
    from agents.task.path import pm

    # Send initial status
    try:
        task = await handler.get_task(task_id, history_length)
        event = TaskStatusUpdateEvent.from_task(
            task, final=A2ATaskState.is_terminal(task.status.state))
        yield _format_sse_event(event)

        # If already terminal, we're done
        if event.final:
            return

    except Exception as e:
        yield _format_sse_error(str(e))
        return

    # Get session info for feed directory
    agent = handler._get_task_agent()
    if not agent:
        yield _format_sse_error("TaskAgent not available")
        return

    session_info = await agent.get_session_by_id(task_id)
    if not session_info:
        yield _format_sse_error(f"Task {task_id} not found")
        return

    user_id = session_info.get("user_id")
    feed_dir = pm().get_subdir(task_id, "feed", user_id=user_id)

    # Track seen files to avoid duplicates. B9: the terminal set is DERIVED
    # from the one status map (it used to be a hand-kept copy that included the
    # non-existent "error" and omitted nothing usefully) — a status added to
    # SessionStatus can no longer be terminal here and non-terminal there.
    seen_files = set()
    from api.a2a.task_handler import TERMINAL_SESSION_STATUSES
    terminal_states = TERMINAL_SESSION_STATUSES
    last_status = None

    # Poll for updates
    while True:
        try:
            # Check for new feed files
            if feed_dir and feed_dir.exists():
                for event_file in sorted(feed_dir.glob("*.json")):
                    if event_file.name in seen_files:
                        continue

                    seen_files.add(event_file.name)

                    try:
                        with open(event_file) as f:
                            event_data = json.load(f)

                        # Convert to appropriate event type
                        event_type = event_data.get("type", "")
                        data = event_data.get("data", {})

                        # Status events
                        if event_type == "status":
                            new_status = data.get("status", "")
                            if new_status != last_status:
                                last_status = new_status

                                # Refresh full task status
                                task = await handler.get_task(task_id, history_length=0)
                                event = TaskStatusUpdateEvent.from_task(
                                    task,
                                    final=A2ATaskState.is_terminal(task.status.state))
                                yield _format_sse_event(event)

                                if event.final:
                                    return

                        # 019 P4: an approval wait is A2A's NATIVE
                        # input-required state; resolution returns to working.
                        # The session's internal status stays "running" while
                        # blocked inside a step, so force the state here.
                        elif event_type == "awaiting_approval":
                            task = await handler.get_task(task_id, history_length=0)
                            task.status.state = A2ATaskState.INPUT_REQUIRED
                            action_name = data.get("action_name") or "action"
                            task.status.message = A2AMessage(
                                role="agent",
                                parts=[{"kind": "text",
                                        "text": f"Awaiting owner approval for '{action_name}'"}]
                            )
                            yield _format_sse_event(
                                TaskStatusUpdateEvent.from_task(task, final=False)
                            )

                        elif event_type == "approval_resolved":
                            task = await handler.get_task(task_id, history_length=0)
                            task.status.state = A2ATaskState.WORKING
                            decision = data.get("decision") or "resolved"
                            action_name = data.get("action_name") or "action"
                            task.status.message = A2AMessage(
                                role="agent",
                                parts=[{"kind": "text",
                                        "text": f"Approval {decision} for '{action_name}'"}]
                            )
                            yield _format_sse_event(
                                TaskStatusUpdateEvent.from_task(task, final=False)
                            )

                        # Step/action events as status updates
                        elif event_type in ["step", "step_result", "action", "action_result"]:
                            # Create intermediate status message
                            message_text = _extract_event_text(event_type, data)
                            if message_text:
                                task = await handler.get_task(task_id, history_length=0)
                                task.status.message = A2AMessage(
                                    role="agent",
                                    parts=[{"kind": "text", "text": message_text}]
                                )
                                event = TaskStatusUpdateEvent.from_task(task, final=False)
                                yield _format_sse_event(event)

                        # Artifact events
                        elif event_type in ["file_created", "artifact"]:
                            artifact_event = _build_artifact_event(
                                task_id, data, context_id=task.contextId)
                            if artifact_event:
                                yield _format_sse_event(artifact_event)

                    except Exception as e:
                        logger.warning(f"Error parsing feed file {event_file}: {e}")

            # Check current session status
            session_info = await agent.get_session_by_id(task_id)
            if session_info:
                current_status = session_info.get("status", "").lower()
                if current_status in terminal_states:
                    # Send final event
                    task = await handler.get_task(task_id)
                    yield _format_sse_event(
                        TaskStatusUpdateEvent.from_task(task, final=True)
                    )
                    return

            # Poll interval
            await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info(f"SSE stream cancelled for task {task_id}")
            break
        except Exception as e:
            logger.error(f"Error in SSE stream: {e}")
            yield _format_sse_error(str(e))
            break


def _format_sse_event(
    event_data,
    event_type: str = "message",
    request_id=None,
) -> str:
    """Format data as SSE event string.

    Args:
        event_data: Event data (Pydantic model or dict)
        event_type: SSE event type; ``"message"`` defers to the frame's own
            spec ``kind`` (``status-update`` / ``artifact-update``)
        request_id: JSON-RPC id to echo, when the stream was opened by one

    Returns:
        SSE formatted string
    """
    if hasattr(event_data, 'dict'):
        data = event_data.dict()
    else:
        data = event_data

    # B27: the SSE `event:` name carries the frame's SPEC kind, so a client
    # that routes on the event name and one that routes on `result.kind` agree.
    # (It used to be the literal "message" for every status frame.)
    kind = data.get("kind") if isinstance(data, dict) else None
    if event_type == "message" and kind:
        event_type = kind

    # Wrap in JSON-RPC response format. `id` is carried through so a frame can
    # be correlated with the request that opened the stream when the caller
    # supplied one (JSON-RPC 2.0 requires the member to be present).
    response = JSONRPCResponse(result=data, id=request_id)

    return f"event: {event_type}\ndata: {json.dumps(response.dict())}\n\n"


def _format_sse_error(message: str, request_id=None) -> str:
    """Format error as SSE event string."""
    response = JSONRPCResponse(
        error={"code": -32603, "message": message},
        id=request_id,
    )
    return f"event: error\ndata: {json.dumps(response.dict())}\n\n"


def _extract_event_text(event_type: str, data: dict) -> Optional[str]:
    """Extract human-readable text from event data.

    Args:
        event_type: Type of event
        data: Event data

    Returns:
        Extracted text or None
    """
    if event_type == "step":
        return data.get("step_name") or data.get("description")
    elif event_type == "step_result":
        return data.get("result") or data.get("output")
    elif event_type == "action":
        action = data.get("action_type", "")
        return f"Executing: {action}"
    elif event_type == "action_result":
        return data.get("result") or data.get("output")
    return None


def _build_artifact_event(
    task_id: str,
    data: dict,
    context_id: Optional[str] = None,
) -> Optional[TaskArtifactUpdateEvent]:
    """Build artifact update event from feed data.

    Args:
        task_id: Task ID
        data: Event data with file info

    Returns:
        TaskArtifactUpdateEvent or None
    """
    from api.a2a.models import A2AArtifact
    import uuid

    file_path = data.get("path") or data.get("file_path")
    if not file_path:
        return None

    file_name = data.get("filename") or data.get("name") or file_path.split("/")[-1]
    mime_type = data.get("mime_type", "application/octet-stream")

    artifact = A2AArtifact(
        artifactId=str(uuid.uuid4()),
        name=file_name,
        parts=[{
            "kind": "file",
            "file": {
                "uri": f"/api/task/sessions/{task_id}/workspace/{file_path}",
                "name": file_name,
                "mimeType": mime_type
            }
        }],
        metadata=data
    )

    return TaskArtifactUpdateEvent(
        taskId=task_id, contextId=context_id, artifact=artifact)


@router.post("/message/stream")
async def stream_message(
    request_body: SendMessageRequest,
    request: Request
):
    """Stream task events for a new or continued task with payment verification.

    Creates a new task and immediately begins streaming updates.
    Equivalent to message/send but returns SSE stream instead of single response.
    """
    from core.container import DependencyContainer
    from api.payment_verification import verify_payment_for_request, payment_required_response

    # B28: the ONE permissive auth policy, not a fourth inlined copy. The
    # hand-rolled block here drifted from `get_user_permissive`: it never
    # accepted an API-key session (`request.state.authenticated` with no JWT),
    # so a machine caller holding a valid rob_xxx key was 401'd on the stream
    # while the same key worked on every other A2A route.
    user_id = await get_user_permissive(request)

    container = DependencyContainer.get_instance()
    handler = A2ATaskHandler(container)

    # Check if continuing existing task
    task_id = request_body.message.taskId
    if task_id:
        # Continue existing task - already paid
        await handler.send_message(task_id, request_body.message, user_id)
    else:
        # NEW TASK: Verify payment before creation
        try:
            payment_method, details = await verify_payment_for_request(
                request=request,
                cost_credits=1  # Session creation = 1 credit
            )
            logger.info(f"A2A stream payment verified via {payment_method}")
        except HTTPException as e:
            if e.status_code == 402:
                raise HTTPException(
                    status_code=402,
                    detail=payment_required_response(request, cost_credits=1)
                )
            raise

        # Create new task
        task = await handler.create_task(
            request_body,
            user_id,
            request_body.message.contextId
        )
        task_id = task.id

    # Return streaming response
    return StreamingResponse(
        task_event_stream(task_id, handler),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/tasks/{task_id}/stream")
async def stream_task_events(
    task_id: str,
    historyLength: Optional[int] = None,
    request: Request = None
):
    """Stream events for an existing task.

    Establishes SSE connection for real-time task updates.
    Useful for monitoring long-running tasks.
    """
    from core.container import DependencyContainer

    container = DependencyContainer.get_instance()
    handler = A2ATaskHandler(container)

    # Authenticate and enforce ownership before streaming another tenant's events.
    user_id = await get_user_permissive(request)
    try:
        await handler.get_task(task_id, user_id=user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return StreamingResponse(
        task_event_stream(task_id, handler, historyLength),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.post("/tasks/resubscribe")
async def resubscribe_to_task(
    body: TaskResubscriptionRequest,
    request: Request,
):
    """Resubscribe to task events after connection loss.

    Re-establishes SSE stream for existing task.
    May replay missed events based on historyLength.

    This is the A2A protocol's recommended method for
    handling network interruptions.

    B26: the parameters arrive in the BODY (``{"id", "historyLength"}``, the
    spec's ``TaskResubscriptionRequest``). They used to be bare function
    arguments on a POST with no body model, which FastAPI reads as QUERY
    parameters — so a spec-conformant client POSTing the documented body got
    422 on a missing `task_id` query string.
    """
    from core.container import DependencyContainer

    container = DependencyContainer.get_instance()
    handler = A2ATaskHandler(container)

    task_id = body.id
    historyLength = body.historyLength

    # Authenticate and enforce ownership before re-subscribing to task events.
    user_id = await get_user_permissive(request)
    # Verify task exists and is not terminal
    try:
        task = await handler.get_task(task_id, user_id=user_id)
        if A2ATaskState.is_terminal(task.status.state):
            # Return final status immediately
            return StreamingResponse(
                _single_event_stream(task),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive"
                }
            )
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # Return streaming response
    return StreamingResponse(
        task_event_stream(task_id, handler, historyLength),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


async def _single_event_stream(task: A2ATask) -> AsyncGenerator[str, None]:
    """Generator for single final event (for terminal tasks)."""
    event = TaskStatusUpdateEvent.from_task(task, final=True)
    yield _format_sse_event(event)
