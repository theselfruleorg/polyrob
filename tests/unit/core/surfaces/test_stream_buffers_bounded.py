"""M4 → 098 TS-19: a surface that does not live-edit keeps NO per-stream state.

M4 bounded the buffered partial path (a buffer map that the router never
finalized). TS-19 removed that path: a partial on such a surface is dropped, so
there is nothing to bound and nothing to leak.
"""
import pytest

from core.surfaces.surface import Surface
from core.surfaces.envelopes import OutboundMessage


class _S(Surface):
    surface_id = "test"
    capabilities = None

    def __init__(self):
        super().__init__()
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)
        return None

    async def start(self, container):
        return None

    async def stop(self):
        return None

    def _incremental_streaming_enabled(self):
        return False


def _partial(key, text="x"):
    return OutboundMessage(session_key=key, text=text, partial=True, stream_id=key)


@pytest.mark.asyncio
async def test_partials_on_a_non_live_surface_hold_no_state():
    s = _S()
    for i in range(Surface._MAX_LIVE_STREAMS + 50):
        await s.stream(_partial(f"k{i}"))
    for _ in range(100):
        await s.stream(_partial("samekey"))
    assert not hasattr(s, "_stream_buffers")
    assert s._live == {} and s.sent == []
