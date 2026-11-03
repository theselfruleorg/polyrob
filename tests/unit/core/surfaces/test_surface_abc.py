import pytest
from core.surfaces.surface import Surface
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities


class _FakeSurface(Surface):
    def __init__(self):
        super().__init__()
        self.sent = []

    @property
    def surface_id(self):
        return "fake"

    @property
    def capabilities(self):
        return SurfaceCapabilities(supports_streaming=False)

    async def send(self, msg):
        self.sent.append(msg)
        return SendResult(success=True, surface_message_id="m1")

    async def start(self, container):
        pass

    async def stop(self):
        pass


@pytest.mark.asyncio
async def test_default_stream_drops_partials_and_sends_a_finalize_text():
    """098 TS-19: no buffered partial path — the reply arrives through send()."""
    s = _FakeSurface()
    await s.stream(OutboundMessage(session_key="k", text="Hel", partial=True, stream_id="s1"))
    await s.stream(OutboundMessage(session_key="k", text="lo", partial=True, stream_id="s1"))
    assert s.sent == [] and not hasattr(s, "_stream_buffers")
    await s.stream(OutboundMessage(session_key="k", text="", partial=False, stream_id="s1"))
    assert s.sent == []  # an empty finalize sends nothing
    await s.stream(OutboundMessage(session_key="k", text="Hello", partial=False, stream_id="s1"))
    assert len(s.sent) == 1
    assert s.sent[0].text == "Hello"
    assert s.sent[0].partial is False


@pytest.mark.asyncio
async def test_default_identify_returns_none():
    s = _FakeSurface()
    assert await s.identify({"x": 1}) is None


def test_cannot_instantiate_without_required_methods():
    with pytest.raises(TypeError):
        Surface()
