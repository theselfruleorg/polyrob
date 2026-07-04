"""Pure log-sanitization helpers (roadmap P9, decomposition pass 1).

Extracted verbatim from ``Agent`` (service.py) — these are stateless utilities
with no ``self`` dependency, so they belong outside the god-file. ``Agent`` keeps
thin delegating wrappers for its existing call sites.
"""
from __future__ import annotations

from typing import Any

_SANITIZE_DROP_KEYS = {"screenshot", "image", "image_data"}
_MAX_LOG_TEXT = 5000


def sanitize_text_for_log(text: str) -> str:
    """Trim inline base64 images or very long strings for logging."""
    if not text:
        return text
    if "data:image" in text or len(text) > _MAX_LOG_TEXT:
        return "<IMAGE_OR_LARGE_DATA_REMOVED>"
    return text


def sanitize_structure_for_log(data: Any) -> Any:
    """Recursively sanitise nested structures for logging (drops image keys)."""
    if isinstance(data, dict):
        return {
            k: sanitize_structure_for_log(v)
            for k, v in data.items()
            if k not in _SANITIZE_DROP_KEYS
        }
    if isinstance(data, list):
        return [sanitize_structure_for_log(v) for v in data]
    if isinstance(data, str):
        return sanitize_text_for_log(data)
    return data
