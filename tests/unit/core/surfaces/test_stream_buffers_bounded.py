"""M4: Surface.stream() buffers partial=True deltas into _stream_buffers and only frees
them on a partial=False finalize — but the streaming mirror always emits partial=True and
never finalizes through the router, so both the buffer map (per chat key) and each key's
chunk list grew for the daemon's whole lifetime. Both must be bounded.
"""
import pytest

from core.surfaces.surface import Surface
from core.surfaces.envelopes import OutboundMessage


class _S(Surface):
    surface_id = "test"
    capabilities = None

    async def send(self, msg):
        return None

    async def start(self, container):
        return None

    async def stop(self):
        return None

    def _incremental_streaming_enabled(self):
        return False  # force the buffered path


def _partial(key, text="x"):
    return OutboundMessage(session_key=key, text=text, partial=True, stream_id=key)


@pytest.mark.asyncio
async def test_buffer_map_key_count_is_bounded():
    s = _S()
    for i in range(Surface._MAX_LIVE_STREAMS + 50):
        await s.stream(_partial(f"k{i}"))
    assert len(s._stream_buffers) <= Surface._MAX_LIVE_STREAMS


@pytest.mark.asyncio
async def test_per_key_chunk_list_is_bounded():
    s = _S()
    for _ in range(5000):
        await s.stream(_partial("samekey"))
    assert len(s._stream_buffers["samekey"]) <= Surface._MAX_STREAM_CHUNKS
