"""Typed value objects for the Surface contract. Transport-free.

Session keys are CHAT-scoped (Hermes parity): a group chat is one shared session,
a DM is isolated by user. Identity rides on the message, never in the key.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MessageKind(str, Enum):
    AGENT_TEXT = "agent_text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ASK = "ask"
    STREAM_DELTA = "stream_delta"
    SYSTEM_NOTE = "system_note"


@dataclass
class SessionSource:
    surface_id: str
    chat_id: str
    chat_type: str = "dm"          # "dm" | "group" | "channel"
    thread_id: Optional[str] = None


@dataclass
class Identity:
    user_id: str                   # internal user_id (NEVER the raw platform id)
    source: SessionSource
    raw_user_id: Optional[str] = None   # platform id (e.g. tg_id), audit only
    display_name: Optional[str] = None


@dataclass
class InboundMessage:
    text: str
    identity: Identity
    idempotency_key: Optional[str] = None   # e.g. Telegram update_id
    kind: str = "comment"
    media: list = field(default_factory=list)
    reply_to: Optional[str] = None
    raw: Optional[dict] = None              # escape hatch
    internal: bool = False                  # synthetic (self-wake/delegation)


@dataclass
class OutboundMessage:
    session_key: str
    text: str
    kind: MessageKind = MessageKind.AGENT_TEXT
    partial: bool = False                   # True = stream delta; False = committed
    stream_id: Optional[str] = None
    reply_to: Optional[str] = None
    media: list = field(default_factory=list)


@dataclass
class SurfaceCapabilities:
    supports_streaming: bool = False
    supports_edit: bool = False
    supports_interactive_ask: bool = False
    is_multi_tenant: bool = False
    max_message_bytes: int = 4096
    markdown_flavor: str = "none"           # "none" | "markdown_v2" | "html"
    service_window_secs: int = 0            # >0 = business-initiated send window (WhatsApp 24h)
    requires_template_outside_window: bool = False  # outside the window, only templates send


@dataclass
class SendResult:
    success: bool
    surface_message_id: Optional[str] = None
    error: Optional[str] = None
