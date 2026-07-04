"""A2A Protocol Endpoints - JSON-RPC over HTTP.

Implements the A2A JSON-RPC 2.0 interface for task management.
All methods follow the A2A specification naming convention.

Supported Methods:
- message/send: Create new task or continue existing
- tasks/get: Get task status
- tasks/list: List tasks with pagination
- tasks/cancel: Cancel a task
- tasks/resubscribe: Resubscribe to streaming events
- tasks/pushNotificationConfig/*: Push notification management

Reference: https://a2a-protocol.org/latest/specification/
"""

import logging
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

from api.a2a.models import (
    JSONRPCRequest, JSONRPCResponse, JSONRPCError, A2AErrorCode,
    SendMessageRequest, GetTaskRequest, ListTasksRequest,
    CancelTaskRequest, TaskResubscriptionRequest,
    SetPushNotificationConfigRequest, PushNotificationConfig,
    A2AMessage, A2ATask, ListTasksResponse
)
from api.a2a.task_handler import A2ATaskHandler
from api.payment_verification import verify_payment_for_request, payment_required_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/a2a", tags=["a2a"])

# A2A-specific error code for payment required
A2A_ERROR_PAYMENT_REQUIRED = -32402


def get_task_handler(request: Request) -> A2ATaskHandler:
    """Get A2A task handler from container."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    return A2ATaskHandler(container)


# Permissive auth policy (accepts x402, JWT, and API-key auth).
# Delegates to the canonical implementation in api.dependencies.
from api.dependencies import get_user_permissive as get_authenticated_user


@router.post("/rpc")
async def a2a_rpc_endpoint(
    rpc_request: JSONRPCRequest,
    request: Request,
    handler: A2ATaskHandler = Depends(get_task_handler)
):
    """A2A JSON-RPC 2.0 endpoint.

    Handles all A2A protocol methods via JSON-RPC.
    """
    request_id = rpc_request.id

    try:
        user_id = await get_authenticated_user(request)
        method = rpc_request.method
        params = rpc_request.params or {}

        logger.info(f"A2A RPC: {method} from user {user_id}")

        # Route to appropriate handler (pass request for payment verification)
        result = await _handle_rpc_method(
            method=method,
            params=params,
            user_id=user_id,
            handler=handler,
            request=request
        )

        return JSONRPCResponse(
            id=request_id,
            result=result.dict() if hasattr(result, 'dict') else result
        )

    except HTTPException as e:
        # Handle 402 Payment Required specially for JSON-RPC
        if e.status_code == 402:
            return JSONRPCResponse(
                id=request_id,
                error=JSONRPCError(
                    code=A2A_ERROR_PAYMENT_REQUIRED,
                    message="Payment required",
                    data=e.detail if isinstance(e.detail, dict) else {"message": e.detail}
                )
            )
        return JSONRPCResponse(
            id=request_id,
            error=JSONRPCError(
                code=A2AErrorCode.AUTHENTICATION_REQUIRED if e.status_code == 401 else A2AErrorCode.INTERNAL_ERROR,
                message=e.detail
            )
        )
    except ValueError as e:
        return JSONRPCResponse(
            id=request_id,
            error=JSONRPCError(
                code=A2AErrorCode.INVALID_PARAMS,
                message=str(e)
            )
        )
    except Exception as e:
        logger.error(f"A2A RPC error: {e}", exc_info=True)
        return JSONRPCResponse(
            id=request_id,
            error=JSONRPCError(
                code=A2AErrorCode.INTERNAL_ERROR,
                message="Internal error",
                data=str(e)
            )
        )


async def _handle_rpc_method(
    method: str,
    params: dict,
    user_id: str,
    handler: A2ATaskHandler,
    request: Request
):
    """Handle individual RPC methods with payment verification.

    Args:
        method: RPC method name
        params: Method parameters
        user_id: Authenticated user
        handler: A2A task handler
        request: FastAPI request for payment verification

    Returns:
        Method result

    Raises:
        ValueError: Invalid params
        HTTPException: Method not found or payment required
    """
    # Message operations
    if method == "message/send":
        message = A2AMessage(**params.get("message", {}))
        config = params.get("configuration")
        msg_request = SendMessageRequest(message=message, configuration=config)

        # Check if this is a new task or continuation
        if message.taskId:
            # Continuation of existing task - already paid
            return await handler.send_message(message.taskId, message, user_id)
        else:
            # NEW TASK: Verify payment before creation
            try:
                payment_method, details = await verify_payment_for_request(
                    request=request,
                    cost_credits=1  # Session creation = 1 credit
                )
                logger.info(f"A2A RPC task payment verified via {payment_method}: {details}")
            except HTTPException as e:
                if e.status_code == 402:
                    # Return 402 with payment options
                    raise HTTPException(
                        status_code=402,
                        detail=payment_required_response(request, cost_credits=1)
                    )
                raise

            return await handler.create_task(msg_request, user_id, message.contextId)

    # Task operations
    elif method == "tasks/get":
        task_id = params.get("id")
        if not task_id:
            raise ValueError("Missing task id")
        history_length = params.get("historyLength")
        return await handler.get_task(task_id, history_length, user_id=user_id)

    elif method == "tasks/list":
        context_id = params.get("contextId")
        page_token = params.get("pageToken")
        page_size = params.get("pageSize", 20)
        tasks, next_token = await handler.list_tasks(
            user_id, context_id, page_token, page_size
        )
        return ListTasksResponse(tasks=tasks, nextPageToken=next_token)

    elif method == "tasks/cancel":
        task_id = params.get("id")
        if not task_id:
            raise ValueError("Missing task id")
        return await handler.cancel_task(task_id, user_id)

    # Push notification operations
    elif method == "tasks/pushNotificationConfig/set":
        task_id = params.get("taskId")
        config_data = params.get("config", {})
        config = PushNotificationConfig(**config_data)
        await handler.set_push_notification_config(task_id, config, user_id=user_id)
        return {"success": True}

    elif method == "tasks/pushNotificationConfig/get":
        task_id = params.get("taskId")
        config = await handler.get_push_notification_config(task_id, user_id=user_id)
        return config.dict() if config else None

    elif method == "tasks/pushNotificationConfig/delete":
        task_id = params.get("taskId")
        success = await handler.delete_push_notification_config(task_id, user_id=user_id)
        return {"success": success}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Method '{method}' not found"
        )


# =============================================================================
@router.post("/tasks")
async def create_task(
    request_body: SendMessageRequest,
    request: Request,
    handler: A2ATaskHandler = Depends(get_task_handler)
):
    """Create a new A2A task with payment verification.

    RESTful convenience endpoint for task creation.
    Equivalent to message/send RPC method.
    """
    user_id = await get_authenticated_user(request)

    # Verify payment for new task creation
    try:
        payment_method, details = await verify_payment_for_request(
            request=request,
            cost_credits=1
        )
        logger.info(f"A2A REST task payment verified via {payment_method}")
    except HTTPException as e:
        if e.status_code == 402:
            return JSONResponse(
                status_code=402,
                content=payment_required_response(request, cost_credits=1)
            )
        raise

    return await handler.create_task(
        request_body,
        user_id,
        request_body.message.contextId
    )


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    historyLength: Optional[int] = None,
    request: Request = None,
    handler: A2ATaskHandler = Depends(get_task_handler)
):
    """Get A2A task status.

    RESTful convenience endpoint for getting task status.
    Equivalent to tasks/get RPC method.
    """
    # Authenticate and enforce ownership (was previously unauthenticated — IDOR).
    user_id = await get_authenticated_user(request)
    try:
        return await handler.get_task(task_id, historyLength, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/tasks/{task_id}/send")
async def send_to_task(
    task_id: str,
    message: A2AMessage,
    request: Request,
    handler: A2ATaskHandler = Depends(get_task_handler)
):
    """Send a message to an existing A2A task.

    RESTful convenience endpoint for continuing tasks.
    Equivalent to message/send RPC method with taskId.
    """
    user_id = await get_authenticated_user(request)
    message.taskId = task_id  # Ensure ID matches path
    try:
        return await handler.send_message(task_id, message, user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    request: Request,
    handler: A2ATaskHandler = Depends(get_task_handler)
):
    """Cancel an A2A task.

    RESTful convenience endpoint for task cancellation.
    Equivalent to tasks/cancel RPC method.
    """
    user_id = await get_authenticated_user(request)
    try:
        return await handler.cancel_task(task_id, user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tasks")
async def list_tasks(
    contextId: Optional[str] = None,
    pageToken: Optional[str] = None,
    pageSize: int = 20,
    request: Request = None,
    handler: A2ATaskHandler = Depends(get_task_handler)
):
    """List A2A tasks with pagination.

    RESTful convenience endpoint for listing tasks.
    Equivalent to tasks/list RPC method.
    """
    user_id = await get_authenticated_user(request)
    tasks, next_token = await handler.list_tasks(
        user_id, contextId, pageToken, pageSize
    )
    return ListTasksResponse(tasks=tasks, nextPageToken=next_token)


# =============================================================================
@router.post("/tasks/{task_id}/push-config")
async def set_push_config(
    task_id: str,
    config: PushNotificationConfig,
    request: Request,
    handler: A2ATaskHandler = Depends(get_task_handler)
):
    """Set push notification config for a task."""
    user_id = await get_authenticated_user(request)
    await handler.set_push_notification_config(task_id, config, user_id=user_id)
    return {"success": True, "taskId": task_id}


@router.get("/tasks/{task_id}/push-config")
async def get_push_config(
    task_id: str,
    request: Request,
    handler: A2ATaskHandler = Depends(get_task_handler)
):
    """Get push notification config for a task."""
    user_id = await get_authenticated_user(request)
    config = await handler.get_push_notification_config(task_id, user_id=user_id)
    if not config:
        raise HTTPException(status_code=404, detail="No push config found")
    return config


@router.delete("/tasks/{task_id}/push-config")
async def delete_push_config(
    task_id: str,
    request: Request,
    handler: A2ATaskHandler = Depends(get_task_handler)
):
    """Delete push notification config for a task."""
    user_id = await get_authenticated_user(request)
    success = await handler.delete_push_notification_config(task_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="No push config found")
    return {"success": True, "taskId": task_id}
