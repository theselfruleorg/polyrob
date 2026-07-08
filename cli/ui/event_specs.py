"""Built-in extension-event specs registered on the D2 registry (T4-02).

First real consumer of ``cli/ui/event_registry.py``: renders the memory
observability events (`memory_recall` / `memory_write`) when they arrive on the
session feed. Registration happens at import (events.py imports this module),
one ``register_event`` call per type — the whole point of the D2 seam.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from cli.ui.event_registry import EventSpec, Layer, RegisteredEvent, register_event


def _parse(feed_dict: Dict[str, Any]) -> RegisteredEvent:
    return RegisteredEvent(
        type=feed_dict.get("type", "unknown"),
        data=feed_dict.get("data", {}) or {},
        raw=feed_dict,
    )


def _render_memory(event: RegisteredEvent) -> Optional[str]:
    d = event.data or {}
    verb = "recalled" if event.type == "memory_recall" else "wrote"
    scope = str(d.get("scope") or "memory")
    chars = d.get("chars")
    size = f", {chars} chars" if isinstance(chars, int) and chars else ""
    preview = str(d.get("preview") or "").strip()
    tail = f" — {preview}" if preview else ""
    return f"🧠 memory {verb} ({scope}{size}){tail}"


def register_builtin_extension_events() -> None:
    """Idempotent: register_event replaces an existing spec for the same type."""
    for kind in ("memory_recall", "memory_write"):
        register_event(EventSpec(
            type=kind,
            parse=_parse,
            layer=Layer.TRACE,  # scaffolding detail: visible under /verbose
            render_line=_render_memory,
        ))


register_builtin_extension_events()
