"""TelegramSurface over an injected bot, no network."""
import pytest

from surfaces.telegram.surface import TelegramSurface
from core.surfaces.envelopes import OutboundMessage, SurfaceCapabilities


class _FakeMessage:
    def __init__(self, message_id): self.message_id = message_id


class _FakeBot:
    def __init__(self, fail=False, media_fail=False):
        self.sent = []
        self.photos = []
        self.documents = []
        self._fail = fail
        self._media_fail = media_fail
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        if self._fail:
            raise RuntimeError("telegram 400")
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return _FakeMessage(self._next_id)

    async def send_photo(self, chat_id, photo, **kwargs):
        if self._media_fail:
            raise RuntimeError("telegram media 400")
        self._next_id += 1
        self.photos.append({"chat_id": chat_id, "photo": photo, "kwargs": kwargs})
        return _FakeMessage(self._next_id)

    async def send_document(self, chat_id, document, **kwargs):
        if self._media_fail:
            raise RuntimeError("telegram media 400")
        self._next_id += 1
        self.documents.append({"chat_id": chat_id, "document": document, "kwargs": kwargs})
        return _FakeMessage(self._next_id)


def _surface(bot=None):
    return TelegramSurface(bot or _FakeBot())


def test_identity_and_capabilities():
    s = _surface()
    assert s.surface_id == "telegram"
    cap = s.capabilities
    assert cap.is_multi_tenant is True
    assert cap.supports_interactive_ask is True
    assert cap.max_message_bytes == 4096


@pytest.mark.asyncio
async def test_send_resolves_chat_from_dm_key():
    bot = _FakeBot()
    s = TelegramSurface(bot)
    res = await s.send(OutboundMessage(session_key="agent:main:telegram:dm:555:u_abc", text="hello"))
    assert res.success is True
    assert bot.sent[0]["chat_id"] == "555"
    assert bot.sent[0]["text"] == "hello"
    assert bot.sent[0]["kwargs"]["parse_mode"] == "MarkdownV2"
    assert res.surface_message_id is not None


@pytest.mark.asyncio
async def test_send_resolves_chat_from_group_key():
    bot = _FakeBot()
    s = TelegramSurface(bot)
    await s.send(OutboundMessage(session_key="agent:main:telegram:group:999", text="hi"))
    assert bot.sent[0]["chat_id"] == "999"


@pytest.mark.asyncio
async def test_send_resolves_chat_from_direct_shim_key():
    """cron/delivery's shim uses session_key=direct:telegram:{chat_id}."""
    bot = _FakeBot()
    s = TelegramSurface(bot)
    await s.send(OutboundMessage(session_key="direct:telegram:777", text="cron note"))
    assert bot.sent[0]["chat_id"] == "777"


@pytest.mark.asyncio
async def test_send_splits_over_4096():
    bot = _FakeBot()
    s = TelegramSurface(bot)
    big = "x" * 9000
    await s.send(OutboundMessage(session_key="agent:main:telegram:dm:5:u", text=big))
    assert len(bot.sent) == 3  # 4096 + 4096 + 808
    assert all(len(c["text"]) <= 4096 for c in bot.sent)
    assert "".join(c["text"] for c in bot.sent) == big


@pytest.mark.asyncio
async def test_send_failopen_on_bot_error():
    s = TelegramSurface(_FakeBot(fail=True))
    res = await s.send(OutboundMessage(session_key="agent:main:telegram:dm:5:u", text="x"))
    assert res.success is False
    assert res.error


@pytest.mark.asyncio
async def test_buffered_stream_flushes_one_send_on_finalize():
    bot = _FakeBot()
    s = TelegramSurface(bot)
    sk = "agent:main:telegram:dm:5:u"
    await s.stream(OutboundMessage(session_key=sk, text="Hel", partial=True, stream_id="x"))
    await s.stream(OutboundMessage(session_key=sk, text="lo", partial=True, stream_id="x"))
    assert bot.sent == []  # buffered, nothing sent yet
    await s.stream(OutboundMessage(session_key=sk, text="", partial=False, stream_id="x"))
    assert len(bot.sent) == 1
    assert bot.sent[0]["text"] == "Hello"


@pytest.mark.asyncio
async def test_send_escapes_markdown_v2():
    bot = _FakeBot()
    s = TelegramSurface(bot)
    await s.send(OutboundMessage(session_key="agent:main:telegram:dm:5:u", text="a.b-c!(x)"))
    assert bot.sent[0]["kwargs"]["parse_mode"] == "MarkdownV2"
    assert bot.sent[0]["text"] == r"a\.b\-c\!\(x\)"


@pytest.mark.asyncio
async def test_send_splits_after_markdown_v2_escape():
    bot = _FakeBot()
    s = TelegramSurface(bot)
    await s.send(OutboundMessage(session_key="agent:main:telegram:dm:5:u", text="." * 4096))
    assert len(bot.sent) == 2
    assert all(len(c["text"]) <= 4096 for c in bot.sent)
    assert "".join(c["text"] for c in bot.sent) == r"\." * 4096


def test_capabilities_media_out():
    assert _surface().capabilities.media_out is True


@pytest.mark.asyncio
async def test_send_image_media_calls_photo_api_and_still_delivers_text(tmp_path):
    bot = _FakeBot()
    s = TelegramSurface(bot)
    img = tmp_path / "card.png"
    img.write_bytes(b"\x89PNG\r\nfake")
    msg = OutboundMessage(
        session_key="agent:main:telegram:dm:5:u", text="your invoice",
        media=[{"kind": "image", "path": str(img), "caption": "Invoice #1"}],
    )
    res = await s.send(msg)
    assert res.success is True
    assert len(bot.sent) == 1 and bot.sent[0]["text"] == "your invoice"
    assert len(bot.photos) == 1
    assert bot.photos[0]["chat_id"] == "5"
    assert bot.photos[0]["photo"].path == str(img)
    assert "Invoice" in bot.photos[0]["kwargs"]["caption"]
    assert bot.documents == []


@pytest.mark.asyncio
async def test_send_document_media_calls_document_api(tmp_path):
    bot = _FakeBot()
    s = TelegramSurface(bot)
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    msg = OutboundMessage(
        session_key="agent:main:telegram:dm:5:u", text="report attached",
        media=[{"kind": "document", "path": str(doc), "caption": None}],
    )
    res = await s.send(msg)
    assert res.success is True
    assert len(bot.documents) == 1
    assert bot.documents[0]["document"].path == str(doc)
    assert bot.photos == []


@pytest.mark.asyncio
async def test_media_caption_falls_back_to_message_text(tmp_path):
    bot = _FakeBot()
    s = TelegramSurface(bot)
    img = tmp_path / "card.png"
    img.write_bytes(b"fake")
    msg = OutboundMessage(
        session_key="agent:main:telegram:dm:5:u", text="fallback caption text",
        media=[{"kind": "image", "path": str(img), "caption": None}],
    )
    await s.send(msg)
    assert "fallback caption text" in bot.photos[0]["kwargs"]["caption"]


@pytest.mark.asyncio
async def test_media_entry_without_path_is_skipped(tmp_path):
    bot = _FakeBot()
    s = TelegramSurface(bot)
    msg = OutboundMessage(
        session_key="agent:main:telegram:dm:5:u", text="no media here",
        media=[{"subject": "legacy email-only entry"}],
    )
    res = await s.send(msg)
    assert res.success is True
    assert bot.photos == [] and bot.documents == []
    assert bot.sent[0]["text"] == "no media here"


@pytest.mark.asyncio
async def test_media_missing_file_is_skipped_with_warn(tmp_path, caplog):
    bot = _FakeBot()
    s = TelegramSurface(bot)
    missing = tmp_path / "nope.png"
    msg = OutboundMessage(
        session_key="agent:main:telegram:dm:5:u", text="text still here",
        media=[{"kind": "image", "path": str(missing), "caption": None}],
    )
    with caplog.at_level("WARNING"):
        res = await s.send(msg)
    assert res.success is True
    assert bot.photos == []
    assert bot.sent[0]["text"] == "text still here"
    assert any("media" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_media_send_failure_is_failopen_text_still_delivered(tmp_path, caplog):
    bot = _FakeBot(media_fail=True)
    s = TelegramSurface(bot)
    img = tmp_path / "card.png"
    img.write_bytes(b"fake")
    msg = OutboundMessage(
        session_key="agent:main:telegram:dm:5:u", text="text must survive",
        media=[{"kind": "image", "path": str(img), "caption": None}],
    )
    with caplog.at_level("WARNING"):
        res = await s.send(msg)
    assert res.success is True
    assert bot.sent[0]["text"] == "text must survive"
    assert any("media" in r.message.lower() for r in caplog.records)
