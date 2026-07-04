"""Typed media envelope shared by every surface. Inbound media (voice/image/doc) is
normalized to Media so the core transcription/handling seams are transport-agnostic.
Kept intentionally small: bytes XOR url; a surface fills whichever it has cheaply."""
from dataclasses import dataclass
from typing import List, Optional

_KINDS = {"voice", "audio", "image", "video", "document", "sticker"}


@dataclass
class Media:
    kind: str
    mime: Optional[str] = None
    data: Optional[bytes] = None
    url: Optional[str] = None
    caption: Optional[str] = None
    filename: Optional[str] = None
    transcript: Optional[str] = None


def coerce_media(items: list) -> List[Media]:
    out: List[Media] = []
    for it in items or []:
        if isinstance(it, Media):
            out.append(it)
        elif isinstance(it, dict) and it.get("kind") in _KINDS:
            out.append(Media(
                kind=it["kind"], mime=it.get("mime"), data=it.get("data"),
                url=it.get("url"), caption=it.get("caption"), filename=it.get("filename"),
                transcript=it.get("transcript"),
            ))
    return out
