"""The Feishu WS long connection (lark-oapi, ``feishu`` extra).

The wire protocol is protobuf (``pbbp2``) with its own ping/reconnect rules, so
this is the one place the SDK is used — and it is imported INSIDE the thread
function, never at module top.

⚠️ ``lark_oapi.ws.Client.start()`` blocks and drives a MODULE-LEVEL event loop
(``lark_oapi.ws.client.loop``, captured at import). It therefore runs on its own
thread with a fresh loop assigned to that global; each event is handed to the
gateway's loop with ``call_soon_threadsafe`` and NOT awaited, so the frame is
acknowledged at once and a long agent turn never stalls the socket (the platform
redelivers an unacknowledged event). Dedup in the harness absorbs a redelivery.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

EVENT_TYPE = "im.message.receive_v1"
#: Order 0005: a card button press (the card callback, schema 2.0).
CARD_EVENT_TYPE = "card.action.trigger"

#: How often the SDK thread checks for stop().
_STOP_POLL_S = 0.2


def event_payload(data) -> dict:
    """A lark ``CustomizedEvent`` → the plain ``{"header", "event"}`` dict
    :func:`surfaces.feishu.events.parse_event` reads."""
    header = getattr(data, "header", None)
    return {"header": {"event_id": getattr(header, "event_id", None),
                       "event_type": getattr(header, "event_type", None)},
            "event": getattr(data, "event", None) or {}}


class FeishuLongConnection:
    def __init__(self, app_id: str, app_secret: str, *, domain: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain          # the Open-API base URL
        self._thread: Optional[threading.Thread] = None
        #: Set by stop(); read on BOTH threads, so a stop that lands while the
        #: SDK is still importing or connecting is not lost.
        self._stopping = threading.Event()

    async def run(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        main_loop = asyncio.get_running_loop()
        done = asyncio.Event()
        failure: list = []
        tasks: set = set()

        def _schedule(payload: dict) -> None:
            task = main_loop.create_task(handler(payload))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        def _on_event(data) -> None:          # runs on the SDK thread
            if self._stopping.is_set():
                return
            try:
                main_loop.call_soon_threadsafe(_schedule, event_payload(data))
            except RuntimeError:              # the gateway loop is closing
                logger.debug("feishu: event dropped, gateway loop closed")

        def _thread_main() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                if self._stopping.is_set():
                    return
                import lark_oapi as lark
                import lark_oapi.ws.client as ws_client
                ws_client.loop = loop         # see the module note
                dispatcher = (lark.EventDispatcherHandler.builder("", "")
                              .register_p2_customized_event(EVENT_TYPE, _on_event)
                              .register_p2_customized_event(CARD_EVENT_TYPE, _on_event)
                              .build())
                client = lark.ws.Client(self._app_id, self._app_secret,
                                        event_handler=dispatcher, domain=self._domain,
                                        log_level=lark.LogLevel.WARNING)
                if self._stopping.is_set():
                    return

                async def _watch_stop() -> None:
                    # start() runs several run_until_complete() calls in a row
                    # (connect, then disconnect + reconnect on a failure, then
                    # forever); ONE loop.stop() is swallowed by its reconnect
                    # branch. Stopping every tick ends each of them until
                    # start() itself raises out.
                    while True:
                        if self._stopping.is_set():
                            loop.stop()
                        await asyncio.sleep(_STOP_POLL_S)

                watcher = loop.create_task(_watch_stop())  # noqa: F841 — kept by the loop
                client.start()
            except Exception as e:  # noqa: BLE001 — reported to the awaiting run()
                if not self._stopping.is_set():
                    failure.append(e)
            finally:
                try:
                    main_loop.call_soon_threadsafe(done.set)
                except RuntimeError:
                    pass

        self._thread = threading.Thread(target=_thread_main, name="feishu-ws", daemon=True)
        self._thread.start()
        await done.wait()
        if failure and not self._stopping.is_set():
            e = failure[0]
            # The SDK's code/msg are the server's words (e.g. the long-connection
            # mode is off in the console); an arbitrary error's text is not
            # repeated — it may quote the endpoint request.
            code = getattr(e, "code", None)
            msg = e.args[0] if code is not None and e.args else ""
            detail = f" (code {code}: {msg})" if code is not None else ""
            raise RuntimeError(f"feishu long connection ended: {type(e).__name__}{detail}")

    async def stop(self) -> None:
        """The SDK thread's watcher stops its loop within one poll tick."""
        self._stopping.set()
