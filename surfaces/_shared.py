"""Shared polled-inbound tail (030 WS-B3/E2).

The four polling harnesses (discord, slack, signal, x) carried byte-similar
``_route`` bodies: route the inbound, build a deliver closure, run the shared
executor, deliver the immediate reply. ONE tail here; each harness keeps only
its transport-specific ``deliver`` (and optional typing) closure.

Lives in the surfaces tier (not core/) because importing
``surfaces.telegram.harness`` is what registers the shared inbound actor —
core must never import surfaces (layering ratchet).
"""
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


async def route_and_act(container: Any, task_agent: Any, inbound: Any,
                        deliver: Callable, *, spawn: Optional[Callable] = None) -> None:
    """route_inbound → act_on_inbound(deliver=) → deliver the immediate reply."""
    from core.surfaces.dispatcher import route_inbound
    from surfaces.telegram.harness import act_on_inbound  # registers the actor
    from surfaces.telegram.inbound import InboundResult

    decision = await route_inbound(container, inbound)
    reply = await act_on_inbound(
        task_agent, InboundResult(inbound=inbound, decision=decision),
        deliver=deliver, spawn=spawn)
    if reply:
        await deliver(reply)


class TextSink:
    """cron/delivery sink over one async ``send(chat_id, text)`` (best-effort).

    Every surface registered a ``<Name>Sink`` with this exact body; the only
    thing that differed was the client method it called.
    """

    def __init__(self, send, *, label: str) -> None:
        self._send = send
        self._label = label

    async def send_message(self, chat_id, text) -> bool:
        try:
            await self._send(str(chat_id), str(text))
            return True
        except Exception:
            logger.warning("%s.send_message failed for %s", self._label, chat_id,
                           exc_info=True)
            return False


class BaseHarness:
    """The surface-agnostic half of a chat-surface harness.

    A transport supplies parse/run/stop and ``_deliver_to``; this holds the
    container, the task agent, the dedup store and the user directory, and
    does the two steps every harness repeated by hand: drop a redelivery, then
    route the inbound through ``route_and_act`` with a best-effort
    deliver-back closure.
    """

    surface_id: str = ""

    def __init__(self, container: Any, task_agent: Any, dedup: Any) -> None:
        self._container = container
        self._task_agent = task_agent
        self._dedup = dedup
        self._user_directory = container.get_service("user_directory") \
            if container else None

    def _is_duplicate(self, inbound) -> bool:
        key = getattr(inbound, "idempotency_key", None)
        return bool(key) and self._dedup.seen(f"{self.surface_id}:{key}")

    async def _deliver_to(self, target, text: str) -> None:
        """Send *text* back to *target* on this transport."""
        raise NotImplementedError

    async def _before_route(self, target) -> None:
        """Transport hook run before routing (e.g. a typing indicator)."""

    async def _route(self, inbound) -> None:
        target = inbound.identity.source.chat_id

        async def _deliver(text: str) -> None:
            try:
                await self._deliver_to(target, text)
            except Exception:
                logger.warning("%s deliver failed", self.surface_id, exc_info=True)

        try:
            await self._before_route(target)
        except Exception:
            pass
        await route_and_act(self._container, self._task_agent, inbound, _deliver)


def register_surface_and_sink(container: Any, surface: Any, *, sink_name: str,
                              send, sink_label: str) -> None:
    """030 WS-B2: ``register_surface`` enforces the contract, joins the surface
    registry (so ``surface_profile()`` reaches the prompt) AND subscribes to the
    router; the cron/delivery sink is registered once beside it."""
    if container is None:
        return
    from core.surfaces.registry import register_surface
    register_surface(container, surface)
    if container.get_service(sink_name) is None:
        container.register_service(sink_name, TextSink(send, label=sink_label))
