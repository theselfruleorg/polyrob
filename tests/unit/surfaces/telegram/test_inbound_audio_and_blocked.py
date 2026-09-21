"""D25 and D63 — the two ways the voice path took something that was not its own.

* **D25** — `build_inbound_message` gated the ref-less voice shape on
  `extract_voice_file_id`, which also matches `audio`. An uploaded audio FILE
  therefore produced a `Media` with no `ref`: neither downloadable nor
  nameable, so the attachment was LOST while the transcription path (which is
  right to accept both) still transcribed it.
* **D63** — transcription is a PAID network round trip and ran before anything
  had asked whether this sender may speak at all. A member a room owner had
  explicitly BLOCKED could spend the owner's money on every voice note he
  posted, forever, by being ignored slightly later in the pipeline.
"""
import pytest

from surfaces.telegram.inbound import build_inbound_message, process_update


class _Directory:
    def resolve_internal(self, tg_id, surface):
        return f"u_{tg_id}"


def _update(**message):
    base = {"message_id": 1, "chat": {"id": 555, "type": "private"},
            "from": {"id": 555, "username": "rob"}}
    base.update(message)
    return {"update_id": 1, "message": base}


def test_a_voice_note_keeps_its_dedicated_ref_less_shape():
    """Unchanged: `voice_guard` and `voice_echo` have always read `media[0]`."""
    msg = build_inbound_message(
        _update(voice={"file_id": "v1", "duration": 3}), _Directory())
    assert [m.kind for m in msg.media] == ["voice"]
    assert msg.media[0].ref is None


def test_an_audio_file_is_a_real_downloadable_attachment():
    """D25: it used to become the ref-less voice shape and vanish."""
    msg = build_inbound_message(
        _update(audio={"file_id": "a1", "mime_type": "audio/mpeg",
                       "file_name": "song.mp3"}), _Directory())
    kinds = [m.kind for m in msg.media]
    assert kinds == ["audio"]
    assert msg.media[0].ref == "a1"
    assert msg.media[0].filename == "song.mp3"


def test_an_audio_file_with_a_caption_still_routes_as_text():
    msg = build_inbound_message(
        _update(audio={"file_id": "a1"}, caption="listen to this"), _Directory())
    assert msg.text == "listen to this"
    assert msg.media[0].ref == "a1"


# --- D63 --------------------------------------------------------------------

class _Dedup:
    def seen(self, update_id, now=None):
        return False


def _room_update(sender=9911):
    return {"update_id": 2,
            "message": {"message_id": 2,
                        "chat": {"id": -100, "type": "supergroup"},
                        "from": {"id": sender},
                        "voice": {"file_id": "v1"}}}


@pytest.mark.asyncio
async def test_a_blocked_members_voice_note_is_never_transcribed(monkeypatch):
    """The money half. The line is still routed (and still DENIED downstream,
    by the tier resolver, which owns that decision) — it just is not paid for."""
    from surfaces.telegram import inbound as inbound_mod
    calls = []

    async def _transcribe(update):
        calls.append(update)
        return "should never happen"

    monkeypatch.setattr("core.surfaces.group_admin.room_role",
                        lambda c, s, cid, uid: "blocked")
    await process_update(None, _room_update(), dedup=_Dedup(),
                         user_directory=_Directory(),
                         transcribe_voice=_transcribe)
    assert calls == []


@pytest.mark.asyncio
async def test_an_ordinary_members_voice_note_is_still_transcribed(monkeypatch):
    calls = []

    async def _transcribe(update):
        calls.append(update)
        return "hello there"

    monkeypatch.setattr("core.surfaces.group_admin.room_role",
                        lambda c, s, cid, uid: "member")
    await process_update(None, _room_update(), dedup=_Dedup(),
                         user_directory=_Directory(),
                         transcribe_voice=_transcribe)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_dm_never_pays_for_the_room_probe(monkeypatch):
    """The gate is room-only: a DM has no per-chat role to read."""
    from surfaces.telegram import inbound as inbound_mod
    probed = []

    def _role(*a, **kw):
        probed.append(a)
        return "blocked"

    async def _transcribe(update):
        return "hi"

    monkeypatch.setattr("core.surfaces.group_admin.room_role", _role)
    await process_update(None, _update(voice={"file_id": "v1"}),
                         dedup=_Dedup(), user_directory=_Directory(),
                         transcribe_voice=_transcribe)
    assert probed == []


@pytest.mark.asyncio
async def test_a_probe_fault_still_transcribes(monkeypatch):
    """Fail-OPEN: this gate bounds a COST, not an access decision — the tier
    resolver owns access and fails CLOSED. A fault here costs one
    transcription, never a turn."""
    calls = []

    def _boom(*a, **kw):
        raise RuntimeError("roles unreadable")

    async def _transcribe(update):
        calls.append(update)
        return "hello"

    monkeypatch.setattr("core.surfaces.group_admin.room_role", _boom)
    await process_update(None, _room_update(), dedup=_Dedup(),
                         user_directory=_Directory(),
                         transcribe_voice=_transcribe)
    assert len(calls) == 1
