"""The agent's avatar — ONE image slot per instance. A primitive, not a generator.

The instance has a face the owner (or the agent, on an owner turn) SETS from an
image: a file, a URL, or the image of an NFT. The CLI, the console, Telegram's
``/avatar``, the ERC-8004 registration file and the invoice card read it from here,
and the agent reads it through ``agent_avatar``, so it knows it has a face and can
send it. The X/Telegram/Discord PROFILE photos are copies: they change only when
``polyrob avatar push`` (``modules/avatar/push.py``) sends the slot there.

Core generates nothing. The generative collection (the mosaic engine) lives in
the ``polyrob-desk`` repo; a face it renders arrives here as an ordinary image.

Layout: ``<home>/identity/{instance_id}/avatar/`` holds ``avatar.<ext>`` and
``avatar.json`` (``{source, content_type, sha256, bytes, set_at}``).

Every new instance starts with the DEFAULT avatar — the polyrob brand mark
(``assets/brand/polyrob-avatar-512.png``) — so every surface always has a face to
show. An empty slot reads as ``state == "set"`` with ``is_default`` true; ``clear``
returns the instance to it.

⚠️ An unreadable record is NOT an absent avatar. :func:`load_avatar` returns
``state == "unreadable"`` for an image with a broken record (or a record whose
image is gone), and a reader must say so rather than report "not set".

⚠️ Only a known raster or SVG image is accepted, checked by its leading bytes —
never by the file name or a server's content type.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SUBDIR = "avatar"
_META = "avatar.json"
_IDENTITY_SUBDIR = "identity"

#: The largest image the slot accepts. A profile image is never bigger.
MAX_BYTES = 5 * 1024 * 1024

#: ext -> content type. The ext is chosen by :func:`sniff_image`, never the caller.
CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}

#: The avatar every instance has until the owner sets one: the brand mark.
DEFAULT_AVATAR = Path(__file__).resolve().parents[1] / "assets" / "brand" / "polyrob-avatar-512.png"
DEFAULT_SOURCE = "default (the polyrob mark)"

#: Surfaces that take only a raster image (X, Discord, Telegram photos).
RASTER_EXTS = frozenset({"png", "jpg", "gif", "webp"})


class AvatarError(ValueError):
    """The image cannot become the avatar (not an image, too big, unreadable)."""


@dataclass(frozen=True)
class AvatarState:
    """What the slot holds. ``state`` is ``set`` | ``none`` | ``unreadable``.

    ``none`` happens only when the shipped default image is missing too."""

    state: str
    path: Optional[Path] = None
    source: Optional[str] = None
    content_type: Optional[str] = None
    sha256: Optional[str] = None
    set_at: Optional[str] = None
    detail: Optional[str] = None
    is_default: bool = False

    @property
    def is_set(self) -> bool:
        return self.state == "set"

    @property
    def is_raster(self) -> bool:
        return bool(self.path) and self.path.suffix.lstrip(".") in RASTER_EXTS


def avatar_dir(home_dir: Path | str, instance_id: Optional[str] = None) -> Path:
    """``<home>/identity/{instance_id}/avatar``. An unsafe id degrades to the default."""
    from core.instance import DEFAULT_INSTANCE_ID, is_safe_tenant_id
    iid = instance_id or DEFAULT_INSTANCE_ID
    safe = str(iid) if is_safe_tenant_id(iid) else DEFAULT_INSTANCE_ID
    return Path(home_dir) / _IDENTITY_SUBDIR / safe / _SUBDIR


def sniff_image(data: bytes) -> Optional[str]:
    """The ext for ``data`` from its leading bytes, or ``None`` if it is not an image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "svg"
    return None


def load_avatar(home_dir: Path | str, instance_id: Optional[str] = None) -> AvatarState:
    """The slot's state. Never raises."""
    try:
        d = avatar_dir(home_dir, instance_id)
        meta_p = d / _META
        images = sorted(p for p in d.glob("avatar.*") if p.suffix.lstrip(".") in CONTENT_TYPES)
        if not meta_p.is_file():
            if images:
                return AvatarState("unreadable", path=images[0],
                                   detail=f"the image has no record ({meta_p})")
            return _default_state()
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception as e:
            return AvatarState("unreadable", detail=f"{meta_p}: {type(e).__name__}")
        if not isinstance(meta, dict) or not meta.get("file"):
            return AvatarState("unreadable", detail=f"{meta_p}: no image named")
        path = d / Path(str(meta["file"])).name
        if not path.is_file():
            return AvatarState("unreadable", detail=f"the record names {path.name}, "
                                                    f"which is gone")
        return AvatarState(
            "set", path=path, source=meta.get("source"),
            content_type=meta.get("content_type"), sha256=meta.get("sha256"),
            set_at=meta.get("set_at"))
    except Exception as e:
        logger.debug("avatar: slot read failed", exc_info=True)
        return AvatarState("unreadable", detail=f"{type(e).__name__}: {e}")


def _default_state() -> AvatarState:
    if DEFAULT_AVATAR.is_file():
        return AvatarState("set", path=DEFAULT_AVATAR, source=DEFAULT_SOURCE,
                           content_type="image/png", is_default=True)
    return AvatarState("none")


def avatar_image_path(home_dir: Path | str, instance_id: Optional[str] = None) -> Optional[Path]:
    """The image path when the slot is set, else ``None``. Never raises."""
    st = load_avatar(home_dir, instance_id)
    return st.path if st.is_set else None


def set_avatar(home_dir: Path | str, instance_id: Optional[str], data: bytes, *,
               source: str) -> AvatarState:
    """Put ``data`` in the slot (replaces any earlier image). ``source`` says where
    it came from (``file:<name>``, ``url:<url>``, ``nft:<chain>:<contract>:<id>``).

    Raises :class:`AvatarError` for an empty, oversized or non-image payload."""
    if not data:
        raise AvatarError("the image is empty")
    if len(data) > MAX_BYTES:
        raise AvatarError(f"the image is {len(data)} bytes; the limit is {MAX_BYTES}")
    ext = sniff_image(data)
    if ext is None:
        raise AvatarError("that is not a PNG, JPEG, GIF, WebP or SVG image")
    d = avatar_dir(home_dir, instance_id)
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"avatar.{ext}"
    tmp = d / f".avatar.{ext}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, target)
    for old in d.glob("avatar.*"):
        if old != target and old.name != _META and old.suffix.lstrip(".") in CONTENT_TYPES:
            old.unlink(missing_ok=True)
    meta = {
        "file": target.name,
        "source": str(source)[:500],
        "content_type": CONTENT_TYPES[ext],
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "set_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    tmp_meta = d / f".{_META}.tmp"
    tmp_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp_meta, d / _META)
    return load_avatar(home_dir, instance_id)


def set_avatar_from_file(home_dir: Path | str, instance_id: Optional[str],
                         path: Path | str, *, source: Optional[str] = None) -> AvatarState:
    """Read an image file into the slot. Raises :class:`AvatarError`."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise AvatarError(f"no such file: {p}")
    if p.stat().st_size > MAX_BYTES:
        raise AvatarError(f"{p.name} is {p.stat().st_size} bytes; the limit is {MAX_BYTES}")
    return set_avatar(home_dir, instance_id, p.read_bytes(),
                      source=source or f"file:{p.name}")


def clear_avatar(home_dir: Path | str, instance_id: Optional[str] = None) -> bool:
    """Empty the slot (the instance shows the default again). Returns whether
    anything was removed."""
    d = avatar_dir(home_dir, instance_id)
    removed = False
    for p in list(d.glob("avatar.*")):
        if p.suffix.lstrip(".") in CONTENT_TYPES or p.name == _META:
            p.unlink(missing_ok=True)
            removed = True
    return removed


def describe(st: AvatarState) -> str:
    """One line for a status / reply: set (source) / not set / unreadable."""
    if st.state == "set" and st.is_default:
        return f"the default ({DEFAULT_SOURCE.split('(', 1)[1].rstrip(')')}; set your own with `polyrob avatar set <image>`)"
    if st.state == "set":
        return f"set ({st.source or 'unknown source'})"
    if st.state == "unreadable":
        return f"unreadable — {st.detail or 'the record does not parse'}"
    return "not set"


__all__ = [
    "AvatarError", "AvatarState", "CONTENT_TYPES", "DEFAULT_AVATAR", "DEFAULT_SOURCE",
    "MAX_BYTES", "RASTER_EXTS",
    "avatar_dir", "avatar_image_path", "clear_avatar", "describe", "load_avatar",
    "set_avatar", "set_avatar_from_file", "sniff_image",
]
