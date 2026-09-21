"""API models for request/response validation and typing."""

from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, field_validator
from datetime import datetime

class MessageRequest(BaseModel):
    """Request model for sending messages to agents."""
    text: str = Field(..., description="The message text to process")
    # B40: IGNORED by the server, on purpose. `/api/chat/message` takes the
    # identity from the AUTHENTICATED request state only (`api/app.py`, C1) —
    # trusting this field let any authenticated caller bill, and recall memory
    # as, another tenant. Kept on the model so an existing client that still
    # sends it is not rejected; it has no effect.
    user_id: Optional[str] = Field(
        None,
        description=("IGNORED — identity comes from the authenticated request. "
                     "Accepted for backward compatibility only."),
    )
    chat_id: Optional[str] = Field(None, description="Chat/conversation identifier")
    message_id: Optional[str] = Field(None, description="Message identifier")
    platform: Optional[str] = Field("api", description="Platform origin (api, web)")
    chat_type: Optional[str] = Field("private", description="Chat type (private only)")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")
    attachments: Optional[List[Dict[str, Any]]] = Field(default_factory=list, description="Message attachments")
    reply_to: Optional[str] = Field(None, description="Message ID this is replying to")
    session_id: Optional[str] = Field(None, description="Session ID for stateful conversations")


class MessageResponse(BaseModel):
    """Response model for agent messages."""
    success: bool = Field(True, description="Whether the request was successful")
    text: Optional[str] = Field(None, description="Response text from the agent")
    format: Optional[str] = Field("markdown", description="Response format (text, markdown, html)")
    message: Optional[str] = Field(None, description="Status or info message")
    message_id: Optional[str] = Field(None, description="Response message ID")
    conversation_id: Optional[str] = Field(None, description="Conversation ID")
    data: Optional[Dict[str, Any]] = Field(None, description="Additional response data")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Response metadata")
    suggestions: Optional[List[str]] = Field(default_factory=list, description="Response suggestions")
    attachments: Optional[List[Dict[str, Any]]] = Field(default_factory=list, description="Response attachments")
    session_id: Optional[str] = Field(None, description="Session ID if applicable")
    agent_id: Optional[str] = Field(None, description="ID of the agent that handled the request")


class ErrorResponse(BaseModel):
    """Standard error response model."""
    success: bool = Field(False, description="Always False for errors")
    error: str = Field(..., description="Error message")
    code: Optional[str] = Field(None, description="Error code")
    details: Optional[Dict[str, Any]] = Field(None, description="Additional error details")


class RateLimitInfo(BaseModel):
    """Rate limit information model."""
    # Support both field names for compatibility
    requests_remaining: Optional[int] = Field(None, description="Number of requests remaining")
    remaining: Optional[int] = Field(None, description="Number of requests remaining (alternative)")
    reset_time: Optional[datetime] = Field(default_factory=datetime.now, description="When the rate limit resets")
    reset_at: Optional[datetime] = Field(None, description="When the rate limit resets (alternative)")
    limit: int = Field(100, description="Total request limit")
    window: str = Field("minute", description="Rate limit window (minute, hour)")



# B39: `SessionCreateRequest` was DELETED (2026-09-21). It had no endpoint —
# `POST /api/task/sessions` reads a raw dict — and no importer, so it was a
# schema nobody validated against, published in api/README.md, advertising
# `model="gpt-5"` / `provider="openai"` defaults the real path stopped using
# long ago (session creation resolves the operator's configured provider). A
# dead model that contradicts the live default is worse than no model.


class SessionResponse(BaseModel):
    """Response model for session operations."""
    ok: bool = Field(True, description="Whether operation was successful")
    session_id: Optional[str] = Field(None, description="Session identifier")
    task: Optional[str] = Field(None, description="Session task")
    status: Optional[str] = Field(None, description="Session status")
    model: Optional[str] = Field(None, description="LLM model")
    tools: Optional[List[str]] = Field(None, description="Enabled tools")
    webview_url: Optional[str] = Field(None, description="Webview URL for session")
    message: Optional[str] = Field(None, description="Status message")
    error: Optional[str] = Field(None, description="Error message if failed")


class SessionStatusResponse(BaseModel):
    """Response model for session status with user-friendly status."""
    id: str = Field(..., description="Session identifier")
    session_id: str = Field(..., description="Session identifier (compatibility)")
    user_id: str = Field(..., description="User identifier")
    task: str = Field(..., description="Session task")
    
    # User-facing status
    status: str = Field(
        ...,
        description="User-facing status: active (working), idle (ready for follow-up), stopped (cancelled)"
    )
    
    # User capabilities
    can_cancel: bool = Field(
        default=False,
        description="Whether user can cancel this session"
    )
    can_send_message: bool = Field(
        default=True,
        description="Whether user can send messages"
    )
    
    # Configuration
    model: Optional[str] = Field(None, description="LLM model")
    tools: Optional[List[str]] = Field(None, description="Enabled tools")
    created_at: Optional[str] = Field(None, description="Creation timestamp")
    last_updated: Optional[str] = Field(None, description="Last update timestamp")
    config: Optional[Dict[str, Any]] = Field(None, description="Session configuration")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Session metadata")
    webview_url: Optional[str] = Field(None, description="WebView URL for this session")

    # 019 P1: what the agent is doing RIGHT NOW (phase/detail/seconds_in_state/
    # step/call_id), derived from the run-state feed events. None when unknown —
    # session idle since restart, RUN_EVENTS_ENABLED=off, or a REMOTE session
    # owned by another worker (in-process snapshot).
    current_activity: Optional[Dict[str, Any]] = Field(
        None,
        description="Live run activity: {phase, detail, seconds_in_state, step, call_id}; null when unknown/remote"
    )
    
    # Debugging
    internal_status: Optional[str] = Field(
        None,
        description="Internal status for debugging (running, completed, etc.)"
    )
    
    # Legacy compatibility
    agents: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Active agents (legacy)")


#: Message kinds a REMOTE caller may declare (B36).
#:
#: ⚠️ This allow-list EXCLUDES `self_wake` and `delegation_result` on purpose.
#: Those two are FORGED-turn kinds: `_drain_user_messages`
#: (`agents/task/agent/core/user_ingress.py`) stamps
#: `orchestrator._forged_turn_kind` from them, and that stamp is what stops a
#: turn auto-activating a skill, patching an active one, or reaching a
#: high-impact verb. An HTTP caller that could name its own kind could mint a
#: turn the agent treats as machine-originated — or, the other way round,
#: launder a forged turn into a genuine owner turn. `kind` arrives from the
#: network; it is a claim, and this is the list of claims we accept.
#:
#: The exclusion is ASSERTED against the canonical seam
#: (``core.security.forged_turns.FORGED_TURN_KINDS``) at import, so a forged
#: kind added there can never be silently admitted here.
ALLOWED_USER_MESSAGE_KINDS = frozenset({
    # Swept from every producer that reaches the HITL queue
    # (`grep submit_user_message` across webview/ surfaces/ cli/ api/ agents/):
    # the console chat box and the REPL `/steer` both send "comment"
    # (webview/static/app/transcript.js, webview/server.py,
    # cli/ui/commands/h_steer.py), and a plain follow-up is "continuation" —
    # the two taint-CLEARING kinds in agents/task/agent/core/user_ingress.py.
    # Plus the documented API vocabulary below.
    #
    # NOTE: the A2A kinds ("a2a_initial"/"a2a_message") are deliberately absent.
    # They are stamped SERVER-SIDE in api/a2a/task_handler.py and never travel
    # through this model, so admitting them here would only let a REST caller
    # dress a message as protocol traffic.
    "comment",
    "continuation",
    "guidance",
    "feedback",
    "steer",
    "user_message",
    "correction",
    "question",
    "answer",
})


# Pinned to the ONE definition of a forged turn kind — never a second copy.
from core.security.forged_turns import FORGED_TURN_KINDS as _FORGED_TURN_KINDS

assert not (ALLOWED_USER_MESSAGE_KINDS & set(_FORGED_TURN_KINDS)), (
    "a forged turn kind must never be declarable over HTTP"
)


class UserMessage(BaseModel):
    """Request model for sending messages to sessions."""
    session_id: Optional[str] = Field(None, description="Session to send message to (optional, can be in URL path)")
    text: str = Field(..., description="Message text")
    kind: Optional[str] = Field(
        "guidance",
        description=(
            "Message type. One of: "
            + ", ".join(sorted(ALLOWED_USER_MESSAGE_KINDS))
            + ". Internal forged-turn kinds (self_wake, delegation_result) are "
              "refused."
        ),
    )

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: Optional[str]) -> str:
        """Refuse any kind outside the allow-list (422). See B36 above."""
        if value is None or value == "":
            return "guidance"
        if value not in ALLOWED_USER_MESSAGE_KINDS:
            raise ValueError(
                f"unsupported message kind {value!r}; allowed: "
                + ", ".join(sorted(ALLOWED_USER_MESSAGE_KINDS))
            )
        return value
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")
    attached_files: Optional[List[str]] = Field(None, description="List of file paths to attach to this message")
    image_attachments: Optional[List[Dict[str, Any]]] = Field(None, description="Base64 image data for vision (internal use)")