"""098 TS-19: overlapping partials open ONE live bubble; a finalize that races
the opener edits that bubble instead of sending a second message."""
import asyncio

import pytest

from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities
from core.surfaces.surface import Surface


class _Slow(Surface):
    def __init__(self):
        super().__init__()
        self.opened, self.edits, self.sent = [], [], []
        self.gate = asyncio.Event()

    @property
    def surface_id(self):
        return "fake"

    @property
    def capabilities(self):
        return SurfaceCapabilities(supports_streaming=True, supports_edit=True,
                                   max_message_bytes=4000)

    def _incremental_streaming_enabled(self):
        return True

    async def _open_stream_message(self, target, text):
        await self.gate.wait()
        self.opened.append(text)
        return 1

    async def _edit_stream_message(self, target, mid, text):
        self.edits.append(text)

    async def _send_stream_overflow(self, target, text):
        self.sent.append(text)

    async def send(self, msg):
        self.sent.append(msg.text)
        return SendResult(success=True)

    async def start(self, c):
        pass

    async def stop(self):
        pass


def _om(t, partial):
    return OutboundMessage(session_key="k", text=t, partial=partial, stream_id="s")


@pytest.mark.asyncio
async def test_concurrent_partials_open_one_message():
    s = _Slow()
    t1 = asyncio.create_task(s.stream(_om("a", True)))
    await asyncio.sleep(0)
    t2 = asyncio.create_task(s.stream(_om("b", True)))
    await asyncio.sleep(0)
    s.gate.set()
    await asyncio.gather(t1, t2)
    assert s.opened == ["a"]
    await s.stream(_om("c", False))
    assert s.sent == [] and s.edits[-1] == "abc"


@pytest.mark.asyncio
async def test_finalize_waits_for_the_opener():
    s = _Slow()
    t1 = asyncio.create_task(s.stream(_om("a", True)))
    await asyncio.sleep(0)
    t2 = asyncio.create_task(s.stream(_om("b", False)))
    await asyncio.sleep(0)
    s.gate.set()
    await asyncio.gather(t1, t2)
    assert s.opened == ["a"] and s.sent == [] and s.edits == ["ab"]
