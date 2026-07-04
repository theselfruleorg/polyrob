"""API models for request/response validation and typing."""

from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, validator
from datetime import datetime

from core.version import get_version


class MessageRequest(BaseModel):
    """Request model for sending messages to agents."""
    text: str = Field(..., description="The message text to process")
    user_id: Optional[str] = Field(None, description="User identifier")
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


class ConversationRequest(BaseModel):
    """Request model for conversation management."""
    user_id: str = Field(..., description="User identifier")
    action: str = Field(..., description="Action to perform (start, continue, end)")
    mode: Optional[str] = Field("standard", description="Conversation mode")
    context: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Conversation context")


class ConversationResponse(BaseModel):
    """Response model for conversation management."""
    success: bool = Field(True, description="Whether the request was successful")
    conversation_id: Optional[str] = Field(None, description="Conversation identifier")
    status: Optional[str] = Field(None, description="Conversation status")
    mode: Optional[str] = Field(None, description="Active conversation mode")
    context: Optional[Dict[str, Any]] = Field(None, description="Updated conversation context")


class RateLimitInfo(BaseModel):
    """Rate limit information model."""
    # Support both field names for compatibility
    requests_remaining: Optional[int] = Field(None, description="Number of requests remaining")
    remaining: Optional[int] = Field(None, description="Number of requests remaining (alternative)")
    reset_time: Optional[datetime] = Field(default_factory=datetime.now, description="When the rate limit resets")
    reset_at: Optional[datetime] = Field(None, description="When the rate limit resets (alternative)")
    limit: int = Field(100, description="Total request limit")
    window: str = Field("minute", description="Rate limit window (minute, hour)")
    
    class Config:
        json_encoders = {
            datetime: lambda dt: dt.isoformat() if dt else None
        }


class AgentCapability(BaseModel):
    """Agent capability information."""
    id: str = Field(..., description="Agent identifier")
    name: str = Field(..., description="Agent display name")
    version: Optional[str] = Field(default_factory=get_version, description="Agent version")
    enabled: bool = Field(True, description="Whether agent is enabled")
    features: List[str] = Field(default_factory=list, description="List of agent features")
    required_services: List[str] = Field(default_factory=list, description="Required services")
    optional_services: List[str] = Field(default_factory=list, description="Optional services")
    description: Optional[str] = Field(None, description="Agent description")


class HealthResponse(BaseModel):
    """Health check response model."""
    status: str = Field(..., description="Health status (healthy, degraded, unhealthy)")
    version: Optional[str] = Field(None, description="API version")
    services: Dict[str, Dict[str, Any]] = Field(default_factory=dict, description="Service health status")
    timestamp: datetime = Field(default_factory=datetime.now, description="Health check timestamp")


class SessionCreateRequest(BaseModel):
    """Request model for creating AutoV2 sessions."""
    user_id: str = Field(..., description="User identifier")
    task: str = Field(..., description="Task to execute")
    model: Optional[str] = Field("gpt-5", description="LLM model to use")
    provider: Optional[str] = Field("openai", description="LLM provider")
    tools: Optional[List[str]] = Field(default_factory=list, description="Tools to enable")
    max_steps: Optional[int] = Field(50, description="Maximum execution steps")
    temperature: Optional[float] = Field(0.0, description="LLM temperature")
    use_vision: Optional[bool] = Field(True, description="Enable vision capabilities")
    session_config: Optional[Dict[str, Any]] = Field(None, description="Additional session configuration")


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
    
    # Debugging
    internal_status: Optional[str] = Field(
        None,
        description="Internal status for debugging (running, completed, etc.)"
    )
    
    # Legacy compatibility
    agents: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Active agents (legacy)")


class UserMessage(BaseModel):
    """Request model for sending messages to sessions."""
    session_id: Optional[str] = Field(None, description="Session to send message to (optional, can be in URL path)")
    text: str = Field(..., description="Message text")
    kind: Optional[str] = Field("guidance", description="Message type (guidance, feedback)")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")
    attached_files: Optional[List[str]] = Field(None, description="List of file paths to attach to this message")
    image_attachments: Optional[List[Dict[str, Any]]] = Field(None, description="Base64 image data for vision (internal use)")