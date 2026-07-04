"""P2a: CLISurface conforms the CLI to the Surface contract.

The renderer stays the pixel owner; CLISurface is a thin transport that forwards
the unified outbound stream into the renderer's existing on_stream_delta seam,
preserving the legacy main-agent filtering (sub-agent deltas must not interleave
into the answer box). Semantic golden: the ordered (partial, text) the renderer
receives via the surface equals what the legacy make_stream_callback produced.
"""
import pytest

from cli.cli_surface import CLISurface
from core.surfaces.envelopes import OutboundMessage, SurfaceCapabilities, MessageKind


class _FakeRenderer:
    def __init__(self):
        self.deltas = []

    def on_stream_delta(self, delta: str) -> None:
        self.deltas.append(delta)


def _msg(text, partial=True, stream_id="agent:main:cli:dm:local:u1:0"):
    return OutboundMessage(session_key="agent:main:cli:dm:local:u1", text=text,
                           partial=partial, stream_id=stream_id)


def test_surface_identity_and_capabilities():
    s = CLISurface(_FakeRenderer())
    assert s.surface_id == "cli"
    cap = s.capabilities
    assert cap.supports_streaming is True
    assert cap.supports_interactive_ask is True
    assert cap.is_multi_tenant is False


@pytest.mark.asyncio
async def test_stream_partials_forward_live_to_renderer():
    r = _FakeRenderer()
    s = CLISurface(r)
    await s.stream(_msg("Hel", partial=True))
    await s.stream(_msg("lo", partial=True))
    assert r.deltas == ["Hel", "lo"]  # live, not buffered-until-finalize


@pytest.mark.asyncio
async def test_stream_finalize_is_noop_in_cli():
    """on_turn_end owns finalize in the CLI; the surface must not double-emit it."""
    r = _FakeRenderer()
    s = CLISurface(r)
    await s.stream(_msg("Hi", partial=True))
    await s.stream(_msg("", partial=False))  # finalize
    assert r.deltas == ["Hi"]  # no extra empty/duplicate delta


@pytest.mark.asyncio
async def test_send_discrete_renders_once():
    r = _FakeRenderer()
    s = CLISurface(r)
    res = await s.send(OutboundMessage(session_key="k", text="done", partial=False))
    assert res.success is True
    assert r.deltas == ["done"]


@pytest.mark.asyncio
async def test_renderer_error_is_fail_open():
    class _BoomRenderer:
        def on_stream_delta(self, d): raise RuntimeError("boom")
    s = CLISurface(_BoomRenderer())
    # must not raise
    await s.stream(_msg("x", partial=True))
    assert (await s.send(OutboundMessage(session_key="k", text="y"))).success is True
