"""P2a: CLISurface — the CLI as a Surface contract consumer.

Thin transport over the existing renderer: the renderer stays the pixel owner
(all the R1/R2/OR-2/OR-7 bubble/dedup logic lives in on_turn_end and is untouched).
CLISurface forwards the unified outbound stream into renderer.on_stream_delta —
the same seam the legacy make_stream_callback used — so flag-ON routing through
the router produces the same ordered delta sequence (the CLI semantic golden).

Sub-agent filtering is NOT done here: the OutboundMessage carries no agent id, so
filtering is a PRODUCER concern (the stream mirror is attached only for the main
agent in _register_stream_callback). Finalize (partial=False stream) is a no-op
because on_turn_end owns the CLI's end-of-turn rendering — emitting here would
double-render. Fully fail-open: a renderer error never propagates into the loop.
"""
import logging
from typing import Any, Callable, Optional

from core.surfaces.surface import Surface
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities

logger = logging.getLogger(__name__)


class CLISurface(Surface):
    def __init__(self, renderer: Any, main_agent_id: Optional[Callable[[], str]] = None) -> None:
        super().__init__()
        self._renderer = renderer
        # Retained for symmetry with make_stream_callback; producer-side filtering
        # is preferred, so this is currently unused by the surface itself.
        self._main_agent_id = main_agent_id

    @property
    def surface_id(self) -> str:
        return "cli"

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            supports_streaming=True,
            supports_edit=True,
            supports_interactive_ask=True,
            is_multi_tenant=False,
            markdown_flavor="none",
        )

    async def send(self, msg: OutboundMessage) -> SendResult:
        try:
            self._renderer.on_stream_delta(msg.text)
        except Exception as e:  # fail-open: rendering must never break the loop
            logger.debug("CLISurface.send render error (ignored): %s", e)
        return SendResult(success=True)

    async def stream(self, msg: OutboundMessage) -> None:
        # CLI renders deltas LIVE (override the buffering default). Finalize
        # (partial=False) is a no-op: on_turn_end owns end-of-turn rendering.
        if not msg.partial:
            return
        try:
            self._renderer.on_stream_delta(msg.text)
        except Exception as e:
            logger.debug("CLISurface.stream render error (ignored): %s", e)

    async def start(self, container) -> None:
        return None

    async def stop(self) -> None:
        return None
