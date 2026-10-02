"""Singular Chat Interface — transport-free surface contract (core).

The public names resolve LAZILY (PEP 562). 064 F1: the surface catalog
(``core.surfaces.catalog``) is read by the CLI entry, and an eager package init
pulled the message router — and through it the memory modules — into every
``polyrob`` start (~150 ms). A submodule attribute (``core.surfaces.access``)
still resolves on first use.
"""
import importlib

_EXPORTS = {
    "MessageKind": "envelopes", "SessionSource": "envelopes", "Identity": "envelopes",
    "InboundMessage": "envelopes", "OutboundMessage": "envelopes",
    "SurfaceCapabilities": "envelopes", "SendResult": "envelopes",
    "Surface": "surface",
    "SessionChatRegistry": "session_chat_registry",
    "build_session_key": "session_chat_registry",
    "MessageRouter": "message_router",
    "SurfaceRegistry": "registry", "register_surface": "registry",
    "is_surface_enabled": "registry",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    mod = _EXPORTS.get(name)
    if mod is not None:
        value = getattr(importlib.import_module(f"{__name__}.{mod}"), name)
        globals()[name] = value
        return value
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{__name__}.{name}":
            raise
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
