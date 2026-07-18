"""Fail-closed scrub gate for trajectory export (design spec §A1 privacy).

Every string in a record passes ``core.secret_scrub.scrub_secret_shapes``
before export; ANY error inside the walk raises :class:`ScrubError` and the
record is refused (fail-closed redaction semantics — never export
content we could not verify as scrubbed). Images/base64 blobs are replaced
with ``[image]`` placeholders. Correspondent-origin content (third-party
data) is detected so callers can exclude those sessions by default.
"""
from __future__ import annotations

import re
from typing import Any

from core.secret_scrub import scrub_secret_shapes

from datagen.record import TrajectoryRecord

_IMAGE_PLACEHOLDER = "[image]"

#: JSON-quoted credential kv (``"password": "..."``) — the core scrubber's kv
#: rule only matches bare ``key = value`` shapes, but tool-call ``arguments``
#: are JSON strings, so quoted keys are a first-class export shape here.
_CRED_KEY = (r"(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|apikey|secret|client_secret|"
             r"password|passwd|access[_-]?token|auth[_-]?token|token|"
             r"authorization|bearer)")
_JSON_KV_RE = re.compile(
    r'(?i)("' + _CRED_KEY + r'"\s*:\s*")([^"]{6,})(")')
_CRED_KEY_RE = re.compile(r"(?i)^" + _CRED_KEY + r"$")


class ScrubError(Exception):
    """Scrubbing failed — the record MUST NOT be exported."""


def strip_images(obj: Any) -> Any:
    """Replace image parts (multimodal dicts, data: URIs) with placeholders."""
    if isinstance(obj, str):
        return _IMAGE_PLACEHOLDER if obj.startswith("data:image/") else obj
    if isinstance(obj, dict):
        if obj.get("type") in ("image", "image_url"):
            return {"type": "text", "text": _IMAGE_PLACEHOLDER}
        return {k: strip_images(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_images(v) for v in obj]
    return obj


def _scrub_str(s: str) -> str:
    s = scrub_secret_shapes(s)
    return _JSON_KV_RE.sub(r"\1<secret>redacted</secret>\3", s)


def _scrub_walk(obj: Any) -> Any:
    if isinstance(obj, str):
        return _scrub_str(obj)
    if isinstance(obj, dict):
        return {k: ("<secret>redacted</secret>"
                    if isinstance(v, str) and len(v) >= 6
                    and isinstance(k, str) and _CRED_KEY_RE.match(k)
                    else _scrub_walk(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_walk(v) for v in obj]
    return obj


def scrub_record(record: TrajectoryRecord) -> TrajectoryRecord:
    """Scrub all exportable text in *record* in place. Fail-closed."""
    try:
        record.task = _scrub_walk(record.task)
        record.messages = _scrub_walk(strip_images(record.messages))
        record.steps = _scrub_walk(strip_images(record.steps))
    except Exception as e:  # noqa: BLE001 — deliberate fail-closed boundary
        raise ScrubError(f"trajectory scrub failed: {e}") from e
    return record


def has_correspondent_content(record: TrajectoryRecord) -> bool:
    """True when any message carries a CORRESPONDENT origin (third-party
    data that must not be exported by default)."""
    for msg in record.messages:
        if isinstance(msg, dict):
            origin = str(msg.get("origin") or "")
            if "CORRESPONDENT" in origin.upper():
                return True
    return False
