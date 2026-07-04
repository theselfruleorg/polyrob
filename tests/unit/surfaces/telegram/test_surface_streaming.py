"""#8: TelegramSurface incremental streaming — live editMessageText as partial deltas
arrive, gated TELEGRAM_INCREMENTAL_STREAM (default OFF = buffered one-send-on-finalize).
The edit cadence is flood-throttled (TELEGRAM_STREAM_EDIT_INTERVAL_SEC); tests set it
to 0 for deterministic per-delta edits.
"""
import pytest

from surfaces.telegram.surface import TelegramSurface
from core.surfaces.envelopes import OutboundMessage

_KEY = "agent:main:telegram:dm:555:u_abc"  # chat id 555


class _FakeBot:
    def __init__(self):
        self.sent = []    # (chat_id, text)
        self.edits = []   # (chat_id, message_id, text)
        self._next_id = 100

    async def send_message(self, chat_id, text, **kw):
        self._next_id += 1
        self.sent.append((str(chat_id), text))
        return type("M", (), {"message_id": self._next_id})()

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kw):
        self.edits.append((str(chat_id), message_id, text))
        return type("M", (), {"message_id": message_id})()


def _om(text, *, partial, stream_id="sid"):
    return OutboundMessage(session_key=_KEY, text=text, partial=partial, stream_id=stream_id)


@pytest.mark.asyncio
async def test_incremental_stream_edits_one_message_in_place(monkeypatch):
    monkeypatch.setenv("TELEGRAM_INCREMENTAL_STREAM", "true")
    monkeypatch.setenv("TELEGRAM_STREAM_EDIT_INTERVAL_SEC", "0")  # edit on every delta
    bot = _FakeBot()
    s = TelegramSurface(bot)
    await s.stream(_om("Hel", partial=True))
    await s.stream(_om("lo", partial=True))
    await s.stream(_om(" world", partial=True))
    await s.stream(_om("!", partial=False))   # finalize
    # Exactly ONE message ever sent (the first partial); the rest are in-place edits.
    assert len(bot.sent) == 1
    assert bot.sent[0] == ("555", "Hel")
    # Edits accumulate and the final edit holds the complete text.
    assert bot.edits, "expected in-place edits"
    assert bot.edits[-1][2] == r"Hello world\!"
    assert bot.edits[-1][0] == "555"


@pytest.mark.asyncio
async def test_flag_off_is_buffered_single_send(monkeypatch):
    monkeypatch.delenv("TELEGRAM_INCREMENTAL_STREAM", raising=False)
    bot = _FakeBot()
    s = TelegramSurface(bot)
    await s.stream(_om("Hello", partial=True))
    await s.stream(_om(" world", partial=True))
    await s.stream(_om("!", partial=False))
    assert bot.edits == []                       # no live edits
    # MarkdownV2 escaping is intentional and always paired with parse_mode="MarkdownV2"
    # (surfaces/telegram/surface.py), so Telegram renders "\!" as a literal "!" to the
    # user — the backslash never leaks. The buffered send is escaped accordingly.
    assert bot.sent == [("555", "Hello world\\!")]  # one buffered send on finalize


@pytest.mark.asyncio
async def test_finalize_without_partials_just_sends(monkeypatch):
    monkeypatch.setenv("TELEGRAM_INCREMENTAL_STREAM", "true")
    bot = _FakeBot()
    s = TelegramSurface(bot)
    await s.stream(_om("just the final", partial=False))
    assert bot.sent == [("555", "just the final")]
    assert bot.edits == []


@pytest.mark.asyncio
async def test_overflow_splits_on_finalize(monkeypatch):
    monkeypatch.setenv("TELEGRAM_INCREMENTAL_STREAM", "true")
    monkeypatch.setenv("TELEGRAM_STREAM_EDIT_INTERVAL_SEC", "0")
    bot = _FakeBot()
    s = TelegramSurface(bot)
    big = "x" * 5000
    await s.stream(_om(big[:100], partial=True))      # opens the live message
    await s.stream(_om(big[100:], partial=False))      # finalize with >4096 total
    # First chunk (4096) lands as an edit on the live message; the overflow is a new send.
    assert bot.edits[-1][2] == big[:4096]
    assert ("555", big[4096:]) in bot.sent


@pytest.mark.asyncio
async def test_discrete_reply_finalizes_live_bubble_no_duplicate(monkeypatch):
    """End-to-end wiring: partials open+edit ONE live message (stable per-turn stream_id),
    then the turn's discrete reply (partial=False, via send()) commits that SAME message
    with the clean text — no second message is sent."""
    monkeypatch.setenv("TELEGRAM_INCREMENTAL_STREAM", "true")
    monkeypatch.setenv("TELEGRAM_STREAM_EDIT_INTERVAL_SEC", "0")
    bot = _FakeBot()
    s = TelegramSurface(bot)
    # Stream deltas (stream_id == session_key, as feed.py now emits).
    await s.stream(OutboundMessage(session_key=_KEY, text="Думаю", partial=True, stream_id=_KEY))
    await s.stream(OutboundMessage(session_key=_KEY, text="…", partial=True, stream_id=_KEY))
    assert len(bot.sent) == 1                      # one live message opened
    # Discrete final reply arrives via send() (partial=False, no stream_id) -> finalize.
    res = await s.send(OutboundMessage(session_key=_KEY, text="Here is the clean answer."))
    assert res.success
    assert len(bot.sent) == 1                      # NO duplicate send
    assert bot.edits[-1][2] == r"Here is the clean answer\."   # bubble committed with clean text
    assert _KEY not in s._live                      # live stream popped


@pytest.mark.asyncio
async def test_discrete_send_is_normal_when_no_live_stream(monkeypatch):
    """With streaming ON but no live stream open (agent replied without streaming), send()
    is a normal new message — not swallowed."""
    monkeypatch.setenv("TELEGRAM_INCREMENTAL_STREAM", "true")
    bot = _FakeBot()
    s = TelegramSurface(bot)
    res = await s.send(OutboundMessage(session_key=_KEY, text="hi"))
    assert res.success
    assert bot.sent == [("555", "hi")] and bot.edits == []


@pytest.mark.asyncio
async def test_discrete_send_unaffected_when_streaming_off(monkeypatch):
    monkeypatch.delenv("TELEGRAM_INCREMENTAL_STREAM", raising=False)
    bot = _FakeBot()
    s = TelegramSurface(bot)
    # Even with a stray _live entry, OFF must not finalize-on-send (normal send).
    await s.stream(OutboundMessage(session_key=_KEY, text="x", partial=True, stream_id=_KEY))
    res = await s.send(OutboundMessage(session_key=_KEY, text="answer"))
    assert res.success
    assert ("555", "answer") in bot.sent           # normal send, not a finalize


@pytest.mark.asyncio
async def test_unchanged_text_not_re_edited(monkeypatch):
    """A finalize whose text equals the last rendered live text must not re-edit
    (Telegram rejects an unchanged edit with 'message is not modified')."""
    monkeypatch.setenv("TELEGRAM_INCREMENTAL_STREAM", "true")
    monkeypatch.setenv("TELEGRAM_STREAM_EDIT_INTERVAL_SEC", "0")
    bot = _FakeBot()
    s = TelegramSurface(bot)
    await s.stream(_om("done", partial=True))   # sends "done"
    n_edits = len(bot.edits)
    await s.stream(_om("", partial=False))       # finalize, text unchanged
    assert len(bot.edits) == n_edits             # no redundant edit
