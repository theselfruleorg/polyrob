"""098 TS-18: a Telegram RetryAfter holds non-final stream edits for the target;
a shorter later retry_after never shortens the active wait."""
import pytest

from core.surfaces.envelopes import OutboundMessage
from surfaces.telegram.surface import TelegramSurface
from tests.support.owner_prefs import set_owner_pref

_KEY = "agent:main:telegram:dm:555:u_abc"


class _Bot:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return type("M", (), {"message_id": 7})()

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kw):
        self.edits.append(text)


def _om(text, partial):
    return OutboundMessage(session_key=_KEY, text=text, partial=partial, stream_id="sid")


class _Flood(Exception):
    def __init__(self, s):
        super().__init__("flood")
        self.retry_after = s


@pytest.mark.asyncio
async def test_nonfinal_edit_skipped_during_flood_wait(monkeypatch):
    set_owner_pref(monkeypatch, "stream.telegram", True)
    monkeypatch.setattr("core.surfaces.config.TELEGRAM_STREAM_EDIT_INTERVAL_SEC", 0.0)
    bot = _Bot()
    s = TelegramSurface(bot)
    await s.stream(_om("a", True))           # opens the bubble
    s._on_stream_error("555", "edit", _Flood(30))
    await s.stream(_om("b", True))
    await s.stream(_om("c", True))
    assert bot.edits == []                   # held during the flood wait
    await s.stream(_om("d", False))          # the final edit still goes
    assert bot.edits and bot.edits[-1] == "abcd"


def test_shorter_retry_after_does_not_shorten_wait(monkeypatch):
    import surfaces.telegram.surface as ts
    now = [1000.0]
    monkeypatch.setattr(ts._time, "monotonic", lambda: now[0])
    s = TelegramSurface(_Bot())
    s._note_flood("555", 30)
    s._note_flood("555", 2)
    now[0] += 10
    assert s._flooded("555")
    now[0] += 25
    assert not s._flooded("555")
