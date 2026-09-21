"""Telegram inbound media (2026-09-13).

Regression anchor: on 2026-09-13 08:00:00 UTC an owner photo arrived as
``text=''`` with ``media=[]`` and ``harness.py``'s empty-content guard dropped it
with no dispatch and no reply. These tests pin the two halves of that bug —
the caption is read, and every attachment kind becomes a ``Media`` entry.
"""
import pytest

from surfaces.telegram.inbound import build_inbound_message
from surfaces.telegram.media import extract_media


class _Dir:
    def resolve_internal(self, raw_id, surface):
        return f"u_{raw_id}"


def _update(msg: dict, update_id: int = 1) -> dict:
    base = {"chat": {"id": 42, "type": "private"}, "from": {"id": 7, "username": "owner"},
            "message_id": 100}
    base.update(msg)
    return {"update_id": update_id, "message": base}


# --- extract_media ---------------------------------------------------------

def test_photo_uses_the_largest_size():
    items = extract_media(_update({"photo": [
        {"file_id": "small", "width": 90, "file_size": 1000},
        {"file_id": "big", "width": 1280, "file_size": 90000},
    ]}))
    assert [i.kind for i in items] == ["image"]
    assert items[0].ref == "big"


def test_document_carries_its_filename_and_mime():
    items = extract_media(_update({"document": {
        "file_id": "doc1", "file_name": "report.pdf", "mime_type": "application/pdf"}}))
    assert items[0].kind == "document"
    assert items[0].filename == "report.pdf"
    assert items[0].mime == "application/pdf"


@pytest.mark.parametrize("field,kind", [
    ("video", "video"), ("animation", "video"), ("video_note", "video"),
    ("sticker", "sticker"), ("audio", "audio"), ("voice", "voice"),
])
def test_every_attachment_kind_is_recognised(field, kind):
    items = extract_media(_update({field: {"file_id": "f1"}}))
    assert items and items[0].kind == kind
    assert items[0].ref == "f1"


def test_a_plain_text_message_has_no_media():
    assert extract_media(_update({"text": "hello"})) == []


def test_caption_rides_on_the_media_entry():
    items = extract_media(_update({"photo": [{"file_id": "p"}], "caption": "look at this"}))
    assert items[0].caption == "look at this"


# --- build_inbound_message -------------------------------------------------

def test_a_captioned_photo_routes_with_the_caption_as_text():
    inbound = build_inbound_message(_update(
        {"photo": [{"file_id": "p"}], "caption": "what is this?"}), _Dir())
    assert inbound.text == "what is this?"
    assert [m.kind for m in inbound.media] == ["image"]


def test_an_uncaptioned_photo_still_produces_media():
    inbound = build_inbound_message(_update({"photo": [{"file_id": "p"}]}), _Dir())
    assert inbound.text == ""
    assert [m.kind for m in inbound.media] == ["image"]


def test_text_wins_over_caption_when_both_are_present():
    inbound = build_inbound_message(
        _update({"text": "typed", "caption": "captioned"}), _Dir())
    assert inbound.text == "typed"


def test_voice_still_produces_exactly_one_voice_media():
    inbound = build_inbound_message(_update({"voice": {"file_id": "v"}}), _Dir())
    assert [m.kind for m in inbound.media] == ["voice"]


def test_an_animated_sticker_is_not_labelled_as_an_image():
    """A .tgs (gzipped Lottie) or .webm sticker given a .webp name would be
    base64'd into a vision block as a broken image."""
    items = extract_media(_update({"sticker": {"file_id": "s1", "is_animated": True}}))
    assert items[0].filename.endswith(".tgs")
    items = extract_media(_update({"sticker": {"file_id": "s2", "is_video": True}}))
    assert items[0].filename.endswith(".webm")
    items = extract_media(_update({"sticker": {"file_id": "s3"}}))
    assert items[0].filename.endswith(".webp")


# ---------------------------------------------------------------------------
# D52 / D76 — a failure is named, and the caption field has a reader
# ---------------------------------------------------------------------------

def test_a_failed_absorb_names_the_files_it_lost():
    """D52: `absorb_for_session` swallowed the fault and returned the message
    exactly as it arrived, so the owner's file vanished with no trace anywhere
    he could see — the silent-drop class the whole media rail exists to end."""
    from core.surfaces.media import Media
    from surfaces.telegram.media import _absorb_failure_text

    out = _absorb_failure_text(
        "have a look",
        [Media(kind="image", ref="f1", filename="chart.png")],
        RuntimeError("disk full"))
    assert out.startswith("have a look")
    assert "chart.png" in out
    assert "could NOT store" in out
    assert "do not act as though you have read them" in out


def test_a_failed_absorb_with_no_caption_is_still_not_empty():
    from core.surfaces.media import Media
    from surfaces.telegram.media import _absorb_failure_text

    out = _absorb_failure_text(
        "", [Media(kind="document", ref="f1", filename="report.pdf")],
        RuntimeError("boom"))
    assert out.strip() and "report.pdf" in out


def test_a_caption_the_turn_already_carries_is_never_repeated():
    """D76: on Telegram a caption is ALREADY promoted to the message text, so
    re-stating it would put the owner's own sentence in front of the model
    twice."""
    from core.surfaces.media import Media
    from surfaces.telegram.media import _with_captions

    media = [Media(kind="image", ref="f1", filename="a.png",
                   caption="what is this?")]
    out = _with_captions("[attached a.png]", media, "what is this?")
    assert out == "[attached a.png]"


def test_a_caption_the_turn_lost_is_carried_through():
    """The field now HAS a reader, for the shapes where the caption is the only
    place the words survive."""
    from core.surfaces.media import Media
    from surfaces.telegram.media import _with_captions

    media = [Media(kind="image", ref="f1", filename="a.png",
                   caption="the third quarter, annotated")]
    out = _with_captions("[attached a.png]", media, "")
    assert "the third quarter, annotated" in out


def test_duplicate_captions_across_an_album_are_said_once():
    from core.surfaces.media import Media
    from surfaces.telegram.media import _with_captions

    media = [Media(kind="image", ref="f1", filename="a.png", caption="both"),
             Media(kind="image", ref="f2", filename="b.png", caption="both")]
    out = _with_captions("[attached 2]", media, "")
    assert out.count("both") == 1
