"""CHAT-4 on WhatsApp: the Telegram voice bound (20 MB / 600 s) holds before
transcription, though Meta declares no duration (the length is measured)."""
import asyncio
import os

from core.surfaces.idempotency import IdempotencyStore
from core.surfaces.media import Media
from core.surfaces.media_access import (MAX_VOICE_BYTES, ogg_duration_seconds,
                                        voice_bytes_too_large)
from surfaces.whatsapp.inbound import WhatsAppInbound


def _ogg(seconds: float) -> bytes:
    granule = int(seconds * 48000).to_bytes(8, "little")
    page = b"OggS" + b"\x00\x00" + granule + b"\x00" * 16
    return page + b"x" * 100 + page


class _UD:
    def resolve_internal(self, raw, surface):
        return "u_" + raw


def _hydrate(tmp_path, data):
    seen = {}

    async def fetch(media_id, *, max_bytes=None):
        seen["max_bytes"] = max_bytes
        return data

    wa = WhatsAppInbound(IdempotencyStore(os.path.join(tmp_path, "i.db")),
                         user_directory=_UD(), media_fetch=fetch)
    m = Media(kind="voice", mime="audio/ogg", url=None, filename="M1")
    asyncio.run(wa.hydrate_media([m]))
    return m, seen


def test_ogg_duration_is_read_from_the_last_page():
    assert abs(ogg_duration_seconds(_ogg(12.5)) - 12.5) < 1e-6
    assert ogg_duration_seconds(b"ID3not-ogg") is None


def test_short_note_is_hydrated_with_the_voice_size_cap(tmp_path):
    m, seen = _hydrate(tmp_path, _ogg(30))
    assert m.data is not None
    assert seen["max_bytes"] == MAX_VOICE_BYTES


def test_long_note_is_not_hydrated(tmp_path):
    m, _ = _hydrate(tmp_path, _ogg(601))
    assert m.data is None


def test_unknown_container_falls_back_to_the_size_cap_only():
    assert not voice_bytes_too_large(b"\xff\xfb" + b"0" * 1000)
    assert voice_bytes_too_large(b"0" * (MAX_VOICE_BYTES + 1))
