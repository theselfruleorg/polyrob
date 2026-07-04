"""TelegramSurface over an injected bot, no network."""
import pytest

from surfaces.telegram.surface import TelegramSurface
from core.surfaces.envelopes import OutboundMessage, SurfaceCapabilities


class _FakeMessage:
    def __init__(self, message_id): self.message_id = message_id


class _FakeBot:
    def __init__(self, fail=False):
        self.sent = []
        self._fail = fail
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        if self._fail:
            raise RuntimeError("telegram 400")
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
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
