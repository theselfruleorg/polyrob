"""D56: a failed outbound media send is SAID, not only logged.

The text of a message carries every fact; the picture is the thing the reader
cannot tell is missing. An invoice CARD that never rendered looked exactly like
an invoice that never had one, and `TelegramSurface._send_media` only WARNed —
so every router-delivered card failed silently. The harness's own reply path
already said it; the surface's did not.
"""
import asyncio

import pytest

from surfaces.telegram.surface import TelegramSurface


class _Bot:
    def __init__(self, *, photo_raises=False):
        self.photo_raises = photo_raises
        self.sent_text = []

    async def send_message(self, chat_id, text, **kw):
        self.sent_text.append(text)
        return type("M", (), {"message_id": 1})()

    async def send_photo(self, chat_id, file, **kw):
        if self.photo_raises:
            raise RuntimeError("telegram refused the upload")
        return type("M", (), {"message_id": 2})()

    async def send_document(self, chat_id, file, **kw):
        return type("M", (), {"message_id": 3})()


@pytest.fixture()
def png(tmp_path):
    p = tmp_path / "card.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(p)


def test_a_missing_file_is_counted(tmp_path):
    bot = _Bot()
    surface = TelegramSurface(bot)
    failed = asyncio.run(surface._send_media(
        "1", [{"kind": "image", "path": str(tmp_path / "gone.png")}], None))
    assert failed == 1


def test_a_refused_upload_is_counted(png):
    bot = _Bot(photo_raises=True)
    surface = TelegramSurface(bot)
    failed = asyncio.run(surface._send_media(
        "1", [{"kind": "image", "path": png}], None))
    assert failed == 1


def test_a_successful_send_counts_nothing(png):
    bot = _Bot()
    surface = TelegramSurface(bot)
    assert asyncio.run(surface._send_media(
        "1", [{"kind": "image", "path": png}], None)) == 0


def test_a_non_renderable_entry_is_not_a_failure():
    """The legacy email-subject shape carries no path and is not media."""
    bot = _Bot()
    surface = TelegramSurface(bot)
    assert asyncio.run(surface._send_media("1", [{"subject": "hi"}], None)) == 0


def test_the_reader_is_told_the_picture_is_missing(png, tmp_path):
    """The whole point: the text lands, and the reader knows a file did not —
    rather than wondering whether they missed it."""
    from core.surfaces.envelopes import OutboundMessage
    bot = _Bot(photo_raises=True)
    surface = TelegramSurface(bot)
    msg = OutboundMessage(session_key="agent:main:telegram:dm:1:u",
                          text="here is your invoice",
                          media=[{"kind": "image", "path": png}])
    asyncio.run(surface.send(msg))
    joined = "\n".join(bot.sent_text)
    assert "here is your invoice" in joined
    assert "could not attach" in joined


def test_nothing_extra_is_said_when_the_media_lands(png):
    from core.surfaces.envelopes import OutboundMessage
    bot = _Bot()
    surface = TelegramSurface(bot)
    msg = OutboundMessage(session_key="agent:main:telegram:dm:1:u",
                          text="here is your invoice",
                          media=[{"kind": "image", "path": png}])
    asyncio.run(surface.send(msg))
    assert not any("could not attach" in t for t in bot.sent_text)
