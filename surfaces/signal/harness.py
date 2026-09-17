"""Assemble the Signal surface: daemon client + SSE loop + routing.

Same shape as the Discord/Slack harnesses: dedup → parse → shared
``route_inbound``/``act_on_inbound`` pipeline → deliver back to the sender.
The shell is ``surfaces._shared.BaseHarness``.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from core.surfaces.idempotency import IdempotencyStore
from surfaces._shared import BaseHarness, TextSink, register_surface_and_sink
from surfaces.signal.client import SignalClient, SignalEventStream
from surfaces.signal.surface import SignalSurface, parse_envelope


def SignalSink(client: SignalClient) -> TextSink:  # noqa: N802 — kept name
    """cron/delivery sink: send a raw text to a number/group (best-effort)."""
    return TextSink(client.send, label="SignalSink")


class SignalHarness(BaseHarness):
    surface_id = "signal"

    def __init__(self, container: Any, task_agent: Any, client: SignalClient,
                 stream: SignalEventStream, dedup: IdempotencyStore) -> None:
        super().__init__(container, task_agent, dedup)
        self._client = client
        self._stream = stream

    async def handle_envelope(self, envelope: dict) -> None:
        inbound = parse_envelope(envelope, self._client.account,
                                 user_directory=self._user_directory)
        if inbound is None or self._is_duplicate(inbound):
            return
        await self._route(inbound)

    async def _deliver_to(self, target, text: str) -> None:
        await self._client.send(target, text)

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
    register_surface_and_sink(container, SignalSurface(client), sink_name="signal_sink",
                              send=client.send, sink_label="SignalSink")
    return SignalHarness(container, task_agent, client, stream, dedup)
