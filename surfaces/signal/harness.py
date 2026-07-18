"""Assemble the Signal surface: daemon client + SSE loop + routing.

Same shape as the Discord/Slack harnesses: dedup → parse → shared
``route_inbound``/``act_on_inbound`` pipeline → deliver back to the sender.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from core.surfaces.idempotency import IdempotencyStore
from surfaces.signal.client import SignalClient, SignalEventStream
from surfaces.signal.surface import SignalSurface, parse_envelope

logger = logging.getLogger(__name__)


class SignalSink:
    """cron/delivery sink: send a raw text to a number/group (best-effort)."""

    def __init__(self, client: SignalClient) -> None:
        self._client = client

    async def send_message(self, chat_id, text) -> bool:
        try:
            await self._client.send(str(chat_id), str(text))
            return True
        except Exception:
            logger.warning("SignalSink.send_message failed for %s", chat_id,
                           exc_info=True)
            return False


class SignalHarness:
    def __init__(self, container: Any, task_agent: Any, client: SignalClient,
                 stream: SignalEventStream, dedup: IdempotencyStore) -> None:
        self._container = container
        self._task_agent = task_agent
        self._client = client
        self._stream = stream
        self._dedup = dedup
        self._user_directory = container.get_service("user_directory") \
            if container else None

    async def handle_envelope(self, envelope: dict) -> None:
        inbound = parse_envelope(envelope, self._client.account,
                                 user_directory=self._user_directory)
        if inbound is None:
            return
        if inbound.idempotency_key and self._dedup.seen(
                f"signal:{inbound.idempotency_key}"):
            return
        await self._route(inbound)

    async def _route(self, inbound) -> None:
        from core.surfaces.dispatcher import route_inbound
        from surfaces.telegram.harness import act_on_inbound
        from surfaces.telegram.inbound import InboundResult

        decision = await route_inbound(self._container, inbound)
        target = inbound.identity.source.chat_id

        async def _deliver(text: str) -> None:
            try:
                await self._client.send(target, text)
            except Exception:
                logger.warning("signal deliver failed", exc_info=True)

        reply = await act_on_inbound(
            self._task_agent,
            InboundResult(inbound=inbound, decision=decision),
            deliver=_deliver,
        )
        if reply:
            await _deliver(reply)

    async def run(self) -> None:
        await self._stream.run(self.handle_envelope)

    async def stop(self) -> None:
        await self._stream.stop()
        await self._client.close()


def build_signal_harness(container: Any, task_agent: Any, *,
                         daemon_url: Optional[str] = None,
                         account: Optional[str] = None,
                         data_dir: str = "data") -> SignalHarness:
    client = SignalClient(daemon_url, account)
    stream = SignalEventStream(client.daemon_url)
    dedup = IdempotencyStore(os.path.join(data_dir, "signal_dedup.db"))
    surface = SignalSurface(client)

    router = container.get_service("message_router") if container else None
    if router is not None:
        router.subscribe("signal", surface)
    if container is not None and container.get_service("signal_sink") is None:
        container.register_service("signal_sink", SignalSink(client))

    return SignalHarness(container, task_agent, client, stream, dedup)
