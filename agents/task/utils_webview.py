"""
Centralized webview URL generation utilities - SINGLE source of truth.
"""

import asyncio
import logging
import os
import weakref
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from core.async_bridge import spawn_retained

logger = logging.getLogger(__name__)


def get_webview_url(session_id: str) -> str:
    """
    Generate WebView URL for monitoring a session.

    This is the SINGLE source of truth for WebView URL generation.
    Reads configuration from BotConfig (which reads from environment).

    Args:
        session_id: Session ID

    Returns:
        Complete WebView URL for the session
    """
    # Get config (reads from environment)
    try:
        from core.config import BotConfig
        config = BotConfig()
        domain = getattr(config, 'webview_domain', None) or os.environ.get('WEBVIEW_DOMAIN', 'localhost:5050')
    except Exception:
        # Fallback to environment if config not available
        domain = os.environ.get('WEBVIEW_DOMAIN', 'localhost:5050')

    # Strip protocol if present
    if domain.startswith('http://'):
        domain = domain[7:]
    elif domain.startswith('https://'):
        domain = domain[8:]

    # Determine protocol based on domain
    # localhost → http, everything else → https
    protocol = 'http' if 'localhost' in domain or '127.0.0.1' in domain else 'https'

    # Clean session ID using PathManager
    from agents.task.path import pm
    clean_id = pm().clean_session_id(session_id)

    # Build URL - simple and consistent
    return f"{protocol}://{domain}/session/{clean_id}"


_STREAM_QUEUE_MAX = 256
_stream_senders: set = set()
_stream_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = weakref.WeakKeyDictionary()


def _stream_client(loop: asyncio.AbstractEventLoop, webview_base: str) -> Any:
    """One client per event loop. A client built per chunk cost 51 ms of CA loading
    on the loop; a loopback target needs no certificate check."""
    client = _stream_clients.get(loop)
    if client is None:
        import httpx
        host = urlsplit(webview_base).hostname
        client = httpx.AsyncClient(verify=host not in {"127.0.0.1", "::1", "localhost"})
        _stream_clients[loop] = client
    return client


def make_webview_stream_callback(
    webview_base: str = "http://127.0.0.1:5050",
    timeout_seconds: float = 2.0,
) -> Callable[[str, str, str, int], Awaitable[None]]:
    """Build an `on_stream_chunk` callback that POSTs LLM chunks to the webview.

    Returned callback signature matches `SessionOrchestrator.on_stream_chunk`:
        async (session_id, agent_id, chunk, step) -> None

    Streaming is internal (server-to-server) — `webview_base` defaults to
    localhost:5050 to bypass nginx, matching the behavior previously hardcoded
    inside the orchestrator. Errors are swallowed to debug logs because
    streaming is non-critical.

    102 N-2: a chunk only joins a bounded queue, so a hung console never holds the
    LLM stream; one sender keeps chunk order, one client serves each event loop, and
    a full queue drops the chunk (the final message does not ride this path).

    Imports httpx lazily so this module stays importable in core-only
    installs that don't have httpx (it is currently a project dep, but the
    lazy import keeps the option open).
    """
    import httpx  # lazy: keeps utils_webview import-clean if httpx absent

    queues: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Queue]" = weakref.WeakKeyDictionary()
    senders: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Task]" = weakref.WeakKeyDictionary()

    async def _send(queue: asyncio.Queue) -> None:
        client = _stream_client(asyncio.get_running_loop(), webview_base)
        while not queue.empty():
            endpoint, body = queue.get_nowait()
            try:
                await client.post(endpoint, json=body, timeout=timeout_seconds)
            except httpx.TimeoutException:
                pass
            except Exception as e:
                logger.debug(f"WebView streaming error: {e}")

    async def stream_to_webview(session_id: str, agent_id: str, chunk: str, step: int) -> None:
        loop = asyncio.get_running_loop()
        queue = queues.get(loop)
        if queue is None:
            queue = queues[loop] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAX)
        body = {"chunk": chunk, "agent_id": agent_id, "step": step}
        try:
            queue.put_nowait((f"{webview_base}/api/webview/sessions/{session_id}/stream", body))
        except asyncio.QueueFull:
            return
        sender = senders.get(loop)
        if sender is None or sender.done():
            senders[loop] = spawn_retained(_send(queue), _stream_senders)

    return stream_to_webview
