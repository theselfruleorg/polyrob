"""Task 1.6 integration test: Telegram inbound populates InboundMessage.media with a
voice Media when the update carries a voice/audio attachment — proving the core seam is
wired (build_inbound_message sets media; voice_needs_guard can use the media list)."""
import pytest
from core.surfaces.media import Media


@pytest.mark.asyncio
async def test_voice_update_yields_voice_media(monkeypatch):
    """build_inbound_message attaches a voice Media when the update has a voice field."""
    from surfaces.telegram.inbound import build_inbound_message

    class _UD:
        def resolve_internal(self, raw, surface):
            return "u1"
        def get_or_create_by_tg_id(self, *a, **k):
            return "u1"

    update = {"update_id": 1, "message": {
        "from": {"id": 42}, "chat": {"id": 42, "type": "private"},
        "voice": {"file_id": "vc1"}, "message_id": 7,
    }}
    inbound = build_inbound_message(update, _UD())
    assert inbound is not None
    assert any(isinstance(m, Media) and m.kind == "voice" for m in inbound.media)


def test_text_update_yields_no_voice_media():
    """A plain-text update must NOT produce voice media."""
    from surfaces.telegram.inbound import build_inbound_message

    class _UD:
        def resolve_internal(self, raw, surface):
            return "u1"

    update = {"update_id": 2, "message": {
        "from": {"id": 42}, "chat": {"id": 42, "type": "private"},
        "text": "hello", "message_id": 8,
    }}
    inbound = build_inbound_message(update, _UD())
    assert inbound is not None
    assert not any(getattr(m, "kind", None) in ("voice", "audio") for m in inbound.media)


def test_core_voice_needs_guard_uses_media_list():
    """The core guard works correctly with the media list from build_inbound_message."""
    from core.surfaces.voice_guard import voice_needs_guard

    voice_media = [Media(kind="voice", mime="audio/ogg")]
    # voice present, no transcript -> guard fires
    assert voice_needs_guard(voice_media, "") is True
    assert voice_needs_guard(voice_media, None) is True
    # voice present, transcript exists -> guard does NOT fire
    assert voice_needs_guard(voice_media, "I said hello") is False
    # no voice media -> guard does NOT fire (even if text is empty)
    assert voice_needs_guard([], "") is False
