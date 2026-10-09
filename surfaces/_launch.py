"""The launch contract between ``polyrob gateway`` and a surface package (064 F1).

``cli/commands/gateway.py`` walks the surface catalog; for every enabled row it
imports ``<spec.module>.launch`` and awaits ``launch(ctx)``. The surface checks
its own credentials (WARN + ``None`` = skipped — never silently ignored, H2),
builds its harness and returns a :class:`Launched`. The gateway owns the rest:
one shared webhook server for every ``webhook=True`` surface, the signal
handlers and the shutdown sweep.

A new surface therefore adds a ``launch.py`` in its own package; the gateway
does not change.
"""
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional


@dataclass
class LaunchContext:
    container: Any
    task_agent: Any
    data_dir: str
    port: int
    warn: Callable[[str], None]
    note: Callable[[str], None]
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Launched:
    #: A long-running coroutine function (poll loop, websocket). None for a
    #: webhook surface — the gateway's shared webhook server carries it.
    run: Optional[Callable[[], Awaitable[Any]]] = None
    stop: Optional[Callable[[], Awaitable[Any]]] = None
    #: Served by the gateway's shared ``/webhooks/<id>`` server.
    webhook: bool = False
    #: Synchronous "stop soon" hook for SIGINT/SIGTERM (before the cancel).
    on_signal: Optional[Callable[[], None]] = None


def plain_http_bind_warning(host: str) -> Optional[str]:
    """A warning when the webhook server binds a NON-loopback address (CHAT-22).

    The server speaks plain HTTP; on a public address the webhook secrets and
    every inbound body cross the network in clear unless a TLS proxy sits in
    front. None for a loopback bind.
    """
    import ipaddress
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return None
    try:
        if ipaddress.ip_address(h).is_loopback:
            return None
    except ValueError:
        pass
    return (f"the webhook server binds {host or '0.0.0.0'} over plain HTTP. Put a TLS "
            "proxy in front of it, or bind 127.0.0.1 behind that proxy (--host)")


def harness_launched(h: Any) -> Launched:
    """The uniform ``run()``/``stop()`` harness shape (discord/slack/signal/x)."""
    return Launched(run=h.run, stop=h.stop)


async def recover_webhook_surfaces(container: Any, task_agent: Any, *,
                                   note: Callable[[str], None],
                                   warn: Callable[[str], None]) -> int:
    """Replay every webhook event acked before a restart, once (064 F4).

    ⚠️ Call it BEFORE the webhook server accepts a request: ``recover`` hands
    back every ``processing`` row, which is only right for rows a dead process
    held. The gateway and every standalone webhook command call THIS, so a
    restart under either keeps the same durability.
    """
    try:
        registered = dict(container.get_service("webhook_surfaces") or {})
    except Exception:
        return 0
    total = 0
    for sid, ws in registered.items():
        if getattr(ws, "ack_before_turn", False) and hasattr(ws, "recover"):
            try:
                n = await ws.recover(container, task_agent)
            except Exception as exc:
                warn(f"{sid}: webhook replay failed: {exc}")
                continue
            if n:
                note(f"{sid}: replayed {n} webhook event(s) accepted before the restart")
                total += n
    return total
