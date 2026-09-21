"""Inbound-attachment seam: bytes arriving on a chat surface land in the session
workspace and are described honestly on the turn (2026-09-13 media rail).

The console already had this pipeline inside ``api/task_http_api.py``; these tests
pin the core seam every surface now shares.
"""
import base64
import os

import pytest

from core.surfaces.inbound_attachments import (
    inbound_media_enabled,
    inject_file_content,
    persist_inbound_file,
)


@pytest.fixture()
def ws(tmp_path):
    d = tmp_path / "workspace"
    d.mkdir()
    return str(d)


# --- persist_inbound_file --------------------------------------------------

def test_persist_writes_under_inbound_and_returns_relative_path(ws):
    rel, reason = persist_inbound_file(ws, "photo.jpg", b"\xff\xd8\xffdata")
    assert reason is None
    assert rel == os.path.join("inbound", "photo.jpg")
    assert (open(os.path.join(ws, rel), "rb").read()) == b"\xff\xd8\xffdata"


def test_persist_never_escapes_the_workspace(ws):
    rel, reason = persist_inbound_file(ws, "../../etc/passwd", b"x")
    assert rel is not None and reason is None
    real = os.path.realpath(os.path.join(ws, rel))
    assert real.startswith(os.path.realpath(ws) + os.sep)
    assert "passwd" in os.path.basename(real)


def test_persist_refuses_oversize_and_names_the_reason(ws):
    rel, reason = persist_inbound_file(ws, "big.bin", b"0" * 2048, max_mb=0.001)
    assert rel is None
    assert reason and "too large" in reason.lower()


def test_persist_refuses_empty_payload(ws):
    rel, reason = persist_inbound_file(ws, "empty.png", b"")
    assert rel is None
    assert reason


def test_persist_deduplicates_a_repeated_filename(ws):
    a, _ = persist_inbound_file(ws, "shot.png", b"one")
    b, _ = persist_inbound_file(ws, "shot.png", b"two")
    assert a != b
    assert open(os.path.join(ws, a), "rb").read() == b"one"
    assert open(os.path.join(ws, b), "rb").read() == b"two"


def test_persist_gives_an_unnamed_file_a_safe_name(ws):
    rel, reason = persist_inbound_file(ws, "", b"bytes", kind="image")
    assert reason is None
    assert rel and os.path.basename(rel)


# --- inject_file_content ---------------------------------------------------

def _png(ws, name="a.png"):
    raw = base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    rel, _ = persist_inbound_file(ws, name, raw)
    return rel


def test_image_becomes_a_vision_block_and_keeps_the_text(ws):
    rel = _png(ws)
    text, images = inject_file_content(ws, rel, "what is this?")
    assert "what is this?" in text
    assert rel in text
    assert images and images[0]["type"] == "image_url"
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_small_text_file_is_inlined(ws):
    rel, _ = persist_inbound_file(ws, "notes.md", b"# hello\nbody")
    text, images = inject_file_content(ws, rel, "read this")
    assert "# hello" in text
    assert images is None


def test_binary_file_is_referenced_by_path_not_inlined(ws):
    rel, _ = persist_inbound_file(ws, "archive.zip", b"PK\x03\x04" + b"\x00" * 100)
    text, images = inject_file_content(ws, rel, "here")
    assert rel in text
    assert images is None


def test_missing_file_is_reported_not_silently_dropped(ws):
    text, images = inject_file_content(ws, "inbound/nope.png", "hi")
    assert "hi" in text
    assert "not found" in text.lower()
    assert images is None


def test_a_path_outside_the_workspace_is_refused(ws, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("token")
    text, images = inject_file_content(ws, "../secret.txt", "read")
    assert images is None
    assert "token" not in text


def test_oversize_image_is_refused_with_a_named_reason(ws):
    rel, _ = persist_inbound_file(ws, "huge.png", b"\x89PNG" + b"0" * 4096, max_mb=10)
    text, images = inject_file_content(ws, rel, "look", max_image_mb=0.001)
    assert images is None
    assert "too large" in text.lower()


# --- flag ------------------------------------------------------------------

def test_inbound_media_is_on_by_default(monkeypatch):
    monkeypatch.delenv("INBOUND_MEDIA_ENABLED", raising=False)
    assert inbound_media_enabled() is True


def test_inbound_media_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("INBOUND_MEDIA_ENABLED", "false")
    assert inbound_media_enabled() is False


# --- absorb_inbound_media --------------------------------------------------

import base64 as _b64

from core.surfaces.media import Media
from core.surfaces.inbound_attachments import absorb_inbound_media

_PNG = _b64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _fetch(mapping):
    async def fetch(media):
        return mapping.get(media.ref)
    return fetch


@pytest.mark.asyncio
async def test_absorb_stores_an_image_and_returns_a_vision_block(ws):
    media = [Media(kind="image", ref="f1", filename="shot.png", mime="image/png")]
    text, images = await absorb_inbound_media(
        media, ws, fetch_bytes=_fetch({"f1": _PNG}), base_text="what is this?")
    assert "what is this?" in text
    assert images and len(images) == 1
    assert media[0].path == os.path.join("inbound", "shot.png")
    assert os.path.exists(os.path.join(ws, media[0].path))


@pytest.mark.asyncio
async def test_absorb_names_a_file_it_could_not_download(ws):
    media = [Media(kind="document", ref="gone", filename="report.pdf")]
    text, images = await absorb_inbound_media(
        media, ws, fetch_bytes=_fetch({}), base_text="see attached")
    assert "see attached" in text
    assert "report.pdf" in text
    assert images is None
    assert media[0].path is None


@pytest.mark.asyncio
async def test_absorb_skips_voice_media(ws):
    media = [Media(kind="voice", mime="audio/ogg", transcript="hello")]
    text, images = await absorb_inbound_media(
        media, ws, fetch_bytes=_fetch({}), base_text="hi")
    assert text == "hi"
    assert images is None


@pytest.mark.asyncio
async def test_absorb_caps_the_number_of_files_and_says_so(ws, monkeypatch):
    monkeypatch.setenv("INBOUND_MEDIA_MAX_FILES", "2")
    media = [Media(kind="image", ref=f"f{i}", filename=f"p{i}.png") for i in range(4)]
    text, images = await absorb_inbound_media(
        media, ws, fetch_bytes=_fetch({f"f{i}": _PNG for i in range(4)}), base_text="")
    assert images and len(images) == 2
    assert "2 more" in text


@pytest.mark.asyncio
async def test_absorb_with_the_flag_off_names_the_files_it_did_not_read(ws, monkeypatch):
    """D51: the flag turns off READING, never TELLING.

    A silent no-op let the agent answer as though the sender had attached
    nothing — the same silent drop the whole rail exists to end. Nothing is
    stored and no vision block is built, but the files are named with the
    reason.
    """
    monkeypatch.setenv("INBOUND_MEDIA_ENABLED", "false")
    media = [Media(kind="image", ref="f1", filename="shot.png")]
    text, images = await absorb_inbound_media(
        media, ws, fetch_bytes=_fetch({"f1": _PNG}), base_text="hi")
    assert text.startswith("hi")
    assert "shot.png" in text
    assert "NOT read" in text
    assert images is None
    # …and nothing was written into the workspace.
    assert not os.path.exists(os.path.join(ws, "inbound"))


@pytest.mark.asyncio
async def test_absorb_with_no_base_text_still_describes_the_file(ws):
    media = [Media(kind="image", ref="f1", filename="shot.png")]
    text, images = await absorb_inbound_media(
        media, ws, fetch_bytes=_fetch({"f1": _PNG}), base_text="")
    assert "shot.png" in text
    assert text.strip()


@pytest.mark.asyncio
async def test_absorb_survives_a_raising_fetch(ws):
    async def boom(media):
        raise RuntimeError("network down")
    media = [Media(kind="image", ref="f1", filename="shot.png")]
    text, images = await absorb_inbound_media(media, ws, fetch_bytes=boom, base_text="hi")
    assert "shot.png" in text
    assert images is None


# --- console upload allowlist ----------------------------------------------

from core.surfaces.inbound_attachments import (
    UPLOAD_EXTENSIONS,
    UPLOAD_MIME_TYPES,
    upload_accept_attribute,
    upload_max_mb,
)


def test_every_vision_extension_is_uploadable():
    from core.surfaces.inbound_attachments import IMAGE_EXTENSIONS
    assert IMAGE_EXTENSIONS <= UPLOAD_EXTENSIONS


def test_svg_is_not_uploadable():
    """An SVG is script-bearing markup, not a picture."""
    assert ".svg" not in UPLOAD_EXTENSIONS
    assert "image/svg+xml" not in UPLOAD_MIME_TYPES


def test_the_accept_attribute_is_derived_from_the_allowlist():
    accept = upload_accept_attribute()
    assert ".png" in accept and ".pdf" in accept and ".mp4" in accept
    assert set(accept.split(",")) == UPLOAD_EXTENSIONS


def test_the_upload_cap_matches_the_chat_surface_cap():
    from core.surfaces.inbound_attachments import inbound_media_max_mb
    assert upload_max_mb() == inbound_media_max_mb()


def test_the_api_endpoint_reads_the_shared_allowlist():
    """Regression: the template allowed .doc/.docx that the endpoint's MIME
    allowlist rejected — three hand-maintained lists, two of them wrong."""
    src = open("api/task_http_api.py").read()
    assert "UPLOAD_EXTENSIONS" in src and "UPLOAD_MIME_TYPES" in src


# --- D54/D55 --------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_overflow_is_NAMED_not_just_counted(ws, monkeypatch):
    """D54: a bare count left the agent unable to say WHICH file it had not
    read, so it could neither ask for it nor admit to the gap."""
    monkeypatch.setenv("INBOUND_MEDIA_MAX_FILES", "1")
    media = [Media(kind="document", ref=f"f{i}", filename=f"part{i}.pdf")
             for i in range(3)]
    text, _ = await absorb_inbound_media(
        media, ws, fetch_bytes=_fetch({f"f{i}": b"%PDF-1.4 data" for i in range(3)}),
        base_text="")
    assert "part1.pdf" in text and "part2.pdf" in text
    assert "2 more" in text


def test_an_undecodable_image_format_says_so_rather_than_posing_as_a_document(ws):
    """D55: `.heic`/`.bmp` fell through to the generic "Attached file" branch,
    which told the agent it was a document — so it neither saw the picture nor
    knew that it was one."""
    rel, _ = persist_inbound_file(ws, "holiday.heic", b"ftypheic" + b"0" * 64)
    text, images = inject_file_content(ws, rel, "what is this?")
    assert images is None
    assert "Attached image" in text
    assert "CANNOT see it" in text
    assert rel in text
