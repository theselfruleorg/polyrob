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
    #: Opaque transport handle the OWNING surface can exchange for bytes later
    #: (a Telegram file_id, an IMAP part index). Distinct from ``url``, which is
    #: a fetchable address anyone can resolve. Lets a surface record what arrived
    #: without paying a download for an update it may still drop.
    ref: Optional[str] = None
    #: Workspace-relative path once the bytes have been stored
    #: (``core.surfaces.inbound_attachments.persist_inbound_file``).
    path: Optional[str] = None


def coerce_media(items: list) -> List[Media]:
    out: List[Media] = []
    for it in items or []:
        if isinstance(it, Media):
            out.append(it)
        elif isinstance(it, dict) and it.get("kind") in _KINDS:
            out.append(Media(
                kind=it["kind"], mime=it.get("mime"), data=it.get("data"),
                url=it.get("url"), caption=it.get("caption"), filename=it.get("filename"),
                transcript=it.get("transcript"), ref=it.get("ref"), path=it.get("path"),
            ))
    return out
