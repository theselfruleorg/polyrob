"""A2A Protocol Models - Pydantic models for A2A communication.

Based on Google's A2A Protocol Specification:
https://a2a-protocol.org/latest/specification/

All models follow the official A2A schema definitions.
"""

from typing import Optional, List, Dict, Any, Union, Literal
from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime
import uuid


class A2ATaskState(str, Enum):
    """A2A Task lifecycle states.

    Non-terminal (active) states:
    - submitted: Initial state, typically transitions immediately to working
    - working: Agent actively processing the task

    Interrupted states (can resume):
    - input-required: Awaiting client information
    - auth-required: Awaiting external authentication

    Terminal states (immutable):
    - completed: Task finished successfully
    - failed: Task processing failed
    - canceled: User-initiated cancellation
    - rejected: Agent declined execution
    - unknown: Unknown/undefined terminal state
    """
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    AUTH_REQUIRED = "auth-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @classmethod
    def is_terminal(cls, state: 'A2ATaskState') -> bool:
        """Check if state is terminal (no further transitions allowed)."""
        return state in [
            cls.COMPLETED, cls.FAILED, cls.CANCELED,
            cls.REJECTED, cls.UNKNOWN
        ]

    @classmethod
    def is_interrupted(cls, state: 'A2ATaskState') -> bool:
        """Check if state is interrupted (awaiting input)."""
        return state in [cls.INPUT_REQUIRED, cls.AUTH_REQUIRED]


class TextPart(BaseModel):
    """Text content part."""
    kind: Literal['text'] = 'text'
    text: str
    metadata: Optional[Dict[str, Any]] = None


class FileWithBytes(BaseModel):
    """File content with inline bytes."""
    bytes: str  # base64-encoded content
    name: Optional[str] = None
    mimeType: Optional[str] = None


class FileWithUri(BaseModel):
    """File content with URI reference."""
    uri: str
    name: Optional[str] = None
    mimeType: Optional[str] = None


class FilePart(BaseModel):
    """File content part."""
    kind: Literal['file'] = 'file'
    file: Union[FileWithBytes, FileWithUri]
    metadata: Optional[Dict[str, Any]] = None


class DataPart(BaseModel):
    """Structured JSON data part."""
    kind: Literal['data'] = 'data'
    data: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = None


# Part union type
Part = Union[TextPart, FilePart, DataPart]


class A2AMessage(BaseModel):
    """A2A Message - Communication payload.

    Messages represent a single turn of communication between
    user/client and agent.
    """
    messageId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: Literal["user", "agent"]
    parts: List[Dict[str, Any]]  # Mixed parts (text, file, data)
    kind: Literal['message'] = 'message'
    taskId: Optional[str] = None
    contextId: Optional[str] = None
    referenceTaskIds: Optional[List[str]] = None
    extensions: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None


class A2AArtifact(BaseModel):
    """A2A Artifact - Output produced by agent.

    Artifacts represent outputs/results of task execution.
    Can contain mixed content types via parts array.
    """
    artifactId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parts: List[Dict[str, Any]]
    name: Optional[str] = None
    description: Optional[str] = None
    extensions: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None

    # Streaming support
    index: Optional[int] = None  # Index within owning task
    append: Optional[bool] = None  # For streaming: append to existing?
    lastChunk: Optional[bool] = None  # For streaming: final chunk?


class A2ATaskStatus(BaseModel):
    """Task status and related messages."""
    state: A2ATaskState
    message: Optional[A2AMessage] = None
    timestamp: Optional[str] = Field(default_factory=lambda: datetime.now().isoformat())


class A2ATask(BaseModel):
    """A2A Task - Fundamental unit of work.

    Tasks encapsulate the entire lifecycle of an agent interaction,
    from initial request to final result.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    contextId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: A2ATaskStatus
    kind: Literal['task'] = 'task'
    history: Optional[List[A2AMessage]] = None
    artifacts: Optional[List[A2AArtifact]] = None
    metadata: Optional[Dict[str, Any]] = None


class SendMessageRequest(BaseModel):
    """Request to send a message (create or continue task)."""
    message: A2AMessage
    configuration: Optional[Dict[str, Any]] = None  # pushNotificationConfig, etc.


class SendMessageResponse(BaseModel):
    """Response from sending a message."""
    task: Optional[A2ATask] = None
    message: Optional[A2AMessage] = None


class GetTaskRequest(BaseModel):
    """Request to get task status."""
    id: str
    historyLength: Optional[int] = None  # None = all, 0 = none, >0 = last N
    metadata: Optional[Dict[str, Any]] = None


class ListTasksRequest(BaseModel):
    """Request to list tasks."""
    contextId: Optional[str] = None
    pageToken: Optional[str] = None
    pageSize: Optional[int] = 20
    includeArtifacts: Optional[bool] = False


class ListTasksResponse(BaseModel):
    """Response for listing tasks."""
    tasks: List[A2ATask]
    nextPageToken: Optional[str] = None


class CancelTaskRequest(BaseModel):
    """Request to cancel a task."""
    id: str


class TaskResubscriptionRequest(BaseModel):
    """Request to resubscribe to task events."""
    id: str
    historyLength: Optional[int] = None


class AuthenticationInfo(BaseModel):
    """Authentication info for webhook callbacks."""
    schemes: List[str]  # e.g., ["Bearer"]
    credentials: Optional[str] = None


class PushNotificationConfig(BaseModel):
    """Configuration for webhook push notifications."""
    url: str  # HTTPS webhook endpoint
    token: Optional[str] = None  # Client-generated validation token
    authentication: Optional[AuthenticationInfo] = None


class SetPushNotificationConfigRequest(BaseModel):
    """Request to set push notification config."""
    taskId: str
    config: PushNotificationConfig


class TaskStatusUpdateEvent(BaseModel):
    """Task status update event in streaming response."""
    task: A2ATask
    final: bool = False  # True if this is the final update


class TaskArtifactUpdateEvent(BaseModel):
    """Artifact update event in streaming response."""
    taskId: str
    artifact: A2AArtifact


class JSONRPCRequest(BaseModel):
    """JSON-RPC 2.0 Request."""
    jsonrpc: str = "2.0"
    method: str
    params: Optional[Dict[str, Any]] = None
    id: Optional[Union[str, int]] = None


class JSONRPCError(BaseModel):
    """JSON-RPC 2.0 Error."""
    code: int
    message: str
    data: Optional[Any] = None


class JSONRPCResponse(BaseModel):
    """JSON-RPC 2.0 Response."""
    jsonrpc: str = "2.0"
    result: Optional[Any] = None
    error: Optional[JSONRPCError] = None
    id: Optional[Union[str, int]] = None


class A2AErrorCode:
    """A2A-specific error codes (JSON-RPC server error range)."""
    # Standard JSON-RPC errors
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # A2A-specific errors (-32000 to -32099)
    TASK_NOT_FOUND = -32000
    TASK_NOT_CANCELABLE = -32001
    PUSH_NOTIFICATION_NOT_SUPPORTED = -32002
    UNSUPPORTED_OPERATION = -32003
    AUTHENTICATION_REQUIRED = -32004
    PAYMENT_REQUIRED = -32005
