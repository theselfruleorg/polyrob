"""030 WS-D1 (finding L1): a Telegram 429 (RetryAfter) on the main send path must
be honored — wait the server-stated delay (bounded) and retry — never answered
with a plain-text resend (which 429s again) and a swallowed loss. The markup-
rejection plain-text retry stays for non-flood errors only.
"""
import pytest

from core.surfaces.envelopes import OutboundMessage
from surfaces.telegram.surface import TelegramSurface


class _RetryAfter(Exception):
    def __init__(self, retry_after):
        super().__init__(f"Flood control exceeded. Retry in {retry_after} seconds")
        self.retry_after = retry_after


class _Sent:
    message_id = 42


class _FloodOnceBot:
    """429 on the first send, success afterwards."""

    def __init__(self, fail_times=1, retry_after=0.01):
        self.calls = []
        self._fails_left = fail_times
        self._retry_after = retry_after

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.calls.append({"text": text, "parse_mode": parse_mode})
        if self._fails_left > 0:
            self._fails_left -= 1
            raise _RetryAfter(self._retry_after)
        return _Sent()


@pytest.mark.asyncio
async def test_retry_after_is_honored_and_the_message_lands():
    bot = _FloodOnceBot(fail_times=1)
    s = TelegramSurface(bot)
    res = await s.send(OutboundMessage(
        session_key="agent:main:telegram:dm:5:u", text="hello **there**"))
    assert res.success is True
    # Two attempts, and the retry kept the ORIGINAL parse mode (no plain-text
    # downgrade — a flood error is not a markup error).
    assert len(bot.calls) == 2
    assert bot.calls[1]["parse_mode"] == bot.calls[0]["parse_mode"] == "HTML"
    assert bot.calls[1]["text"] == bot.calls[0]["text"]


@pytest.mark.asyncio
async def test_persistent_flood_exhausts_and_reports_failure():
    bot = _FloodOnceBot(fail_times=99)
    s = TelegramSurface(bot)
    res = await s.send(OutboundMessage(
        session_key="agent:main:telegram:dm:5:u", text="hello"))
    assert res.success is False
    # Bounded: initial try + the capped retries, not an unbounded loop.
    assert 2 <= len(bot.calls) <= 4


class _MarkupRejectBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.calls.append({"text": text, "parse_mode": parse_mode})
        if parse_mode is not None:
            raise ValueError("can't parse entities")
        return _Sent()


@pytest.mark.asyncio
async def test_markup_rejection_still_downgrades_to_plain_text():
    bot = _MarkupRejectBot()
    s = TelegramSurface(bot)
    res = await s.send(OutboundMessage(
        session_key="agent:main:telegram:dm:5:u", text="broken <tag"))
    assert res.success is True
    assert bot.calls[-1]["parse_mode"] is None
