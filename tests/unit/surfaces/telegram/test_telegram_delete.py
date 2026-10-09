"""0008: a Telegram send reports EVERY message id it produced, and the surface can
delete one of its own messages (Bot API deleteMessage) — fake bot, no network."""
import pytest

from core.surfaces.envelopes import OutboundMessage
from surfaces.telegram.surface import TelegramSurface


class _Msg:
    def __init__(self, mid): self.message_id = mid


class _Bot:
    def __init__(self, delete_error=None):
        self._next = 500
        self.deleted = []
        self._delete_error = delete_error

    async def send_message(self, chat_id, text, **kw):
        self._next += 1
        return _Msg(self._next)

    async def send_document(self, chat_id, document, **kw):
        self._next += 1
        return _Msg(self._next)

    async def delete_message(self, chat_id, message_id):
        if self._delete_error:
            raise RuntimeError(self._delete_error)
        self.deleted.append((chat_id, message_id))
        return True


@pytest.mark.asyncio
async def test_send_reports_every_chunk_id():
    s = TelegramSurface(_Bot())
    res = await s.send(OutboundMessage(session_key="direct:telegram:-1001",
                                       text=("word " * 1500)))  # > one 4096 chunk
    assert res.success
    assert len(res.surface_message_ids) >= 2
    assert res.surface_message_id == res.surface_message_ids[-1]


@pytest.mark.asyncio
async def test_send_reports_media_ids_too(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text("x")
    s = TelegramSurface(_Bot())
    res = await s.send(OutboundMessage(session_key="direct:telegram:-1001", text="hi",
                                       media=[{"path": str(f), "kind": "document"}]))
    assert res.surface_message_ids == ["501", "502"]


@pytest.mark.asyncio
async def test_delete_message_calls_bot_api():
    bot = _Bot()
    res = await TelegramSurface(bot).delete_message("-1001", "77")
    assert res.ok is True
    assert bot.deleted == [("-1001", 77)]


@pytest.mark.asyncio
async def test_delete_failure_is_typed_never_raises():
    bot = _Bot(delete_error="Bad Request: message can't be deleted")
    res = await TelegramSurface(bot).delete_message("-1001", "77")
    assert res.ok is False
    assert "can't be deleted" in res.reason


@pytest.mark.asyncio
async def test_delete_non_numeric_id_refused():
    res = await TelegramSurface(_Bot()).delete_message("-1001", "abc")
    assert res.ok is False
