"""Singular Chat Interface — transport-free surface contract (core)."""
from core.surfaces.envelopes import (
    MessageKind, SessionSource, Identity, InboundMessage,
    OutboundMessage, SurfaceCapabilities, SendResult,
)
from core.surfaces.surface import Surface
from core.surfaces.session_chat_registry import SessionChatRegistry, build_session_key
from core.surfaces.message_router import MessageRouter
from core.surfaces.registry import SurfaceRegistry, register_surface, is_surface_enabled

__all__ = [
    "MessageKind", "SessionSource", "Identity", "InboundMessage",
    "OutboundMessage", "SurfaceCapabilities", "SendResult", "Surface",
    "SessionChatRegistry", "build_session_key", "MessageRouter",
    "SurfaceRegistry", "register_surface", "is_surface_enabled",
]
