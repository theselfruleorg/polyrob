"""OB7: a partial Telegram send is not re-sent from the start.

The outbound dispatcher retried a WHOLE row after a partial send: chunk 1
landed, chunk 2 failed, and every retry posted chunk 1 again. The surface now
reports the text that did NOT go (``SendResult.remaining_text``) and the
dispatcher resumes from there; a failure after the text landed (the "could not
attach" note) no longer fails the row at all.
"""
import os

import pytest

from core.surfaces.envelopes import OutboundMessage, SendResult
from core.surfaces.outbound_dispatcher import OutboundDispatcher
from core.surfaces.outbound_queue import OutboundDeliveryQueue
from surfaces.telegram.surface import TelegramSurface

_KEY = "agent:main:telegram:dm:5:u"


class _Msg:
    def __init__(self, mid): self.message_id = mid


class _FlakyBot:
    """Fails the send_message calls whose 1-based index is in *fail_calls*."""

    def __init__(self, fail_calls=(), media_fail=False):
        self.sent = []
        self.calls = 0
        self._fail = set(fail_calls)
        self._media_fail = media_fail

    async def send_message(self, chat_id, text, **kwargs):
        self.calls += 1
        if self.calls in self._fail:
            raise RuntimeError("Bad Gateway")
        self.sent.append(text)
        return _Msg(100 + self.calls)

    async def send_photo(self, chat_id, photo, **kwargs):
        raise RuntimeError("telegram media 400")

    send_document = send_photo


def _two_chunks():
    return "A" * 4000 + "\n\n" + "B" * 3000


@pytest.mark.asyncio
async def test_chunk_two_failure_reports_the_undelivered_text():
    # Call 2 = chunk 2 (HTML), call 3 = its plain-text retry: both fail.
    bot = _FlakyBot(fail_calls={2, 3})
    res = await TelegramSurface(bot).send(OutboundMessage(session_key=_KEY, text=_two_chunks()))
    assert res.success is False
    assert bot.sent == ["A" * 4000]
    assert res.remaining_text is not None and "A" not in res.remaining_text
    assert res.remaining_text.strip() == "B" * 3000


@pytest.mark.asyncio
async def test_first_chunk_failure_reports_unknown_progress():
    bot = _FlakyBot(fail_calls={1, 2})
    res = await TelegramSurface(bot).send(OutboundMessage(session_key=_KEY, text="hi"))
    assert res.success is False and res.remaining_text is None


@pytest.mark.asyncio
async def test_dispatcher_retry_resumes_without_a_duplicate_chunk_one(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="k", session_key=_KEY, surface_id="telegram",
              dest="5", payload=_two_chunks())
    bot = _FlakyBot(fail_calls={2, 3})
    surface = TelegramSurface(bot)
    d = OutboundDispatcher(q, lambda sid: surface, max_attempts=5, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000)
    assert await d.drain_once(now=0.0) == 0
    assert q.counts()["pending"] == 1
    assert await d.drain_once(now=10_000.0) == 1
    assert q.counts()["delivered"] == 1
    assert bot.sent.count("A" * 4000) == 1          # chunk 1 went exactly once
    assert bot.sent[-1] == "B" * 3000


@pytest.mark.asyncio
async def test_failed_attach_note_after_the_text_does_not_fail_the_row(tmp_path):
    img = tmp_path / "card.png"
    img.write_bytes(b"fake")
    bot = _FlakyBot(fail_calls={2, 3})              # the "could not attach" note
    res = await TelegramSurface(bot).send(OutboundMessage(
        session_key=_KEY, text="the text",
        media=[{"kind": "image", "path": str(img), "caption": None}]))
    assert res.success is True
    assert bot.sent == ["the text"]


class _ScriptedSurface:
    surface_id = "wa"

    def __init__(self, results):
        self._results = list(results)
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg.text)
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_dispatcher_empty_remaining_text_counts_as_delivered(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="k", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _ScriptedSurface([SendResult(success=False, error="note failed", remaining_text="")])
    d = OutboundDispatcher(q, lambda sid: surf, rate_per_sec=1000, burst=1000)
    assert await d.drain_once(now=0.0) == 1
    assert q.counts()["delivered"] == 1


@pytest.mark.asyncio
async def test_dispatcher_unknown_progress_keeps_the_whole_row(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="k", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _ScriptedSurface([SendResult(success=False, error="x"), SendResult(success=True)])
    d = OutboundDispatcher(q, lambda sid: surf, base_backoff=1.0, rate_per_sec=1000, burst=1000)
    await d.drain_once(now=0.0)
    await d.drain_once(now=10_000.0)
    assert surf.sent == ["hi", "hi"]
