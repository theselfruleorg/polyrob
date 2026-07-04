"""
Centralized webview URL generation utilities - SINGLE source of truth.
"""

import logging
import os
from typing import Awaitable, Callable

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

    Imports httpx lazily so this module stays importable in core-only
    installs that don't have httpx (it is currently a project dep, but the
    lazy import keeps the option open).
    """
    import httpx  # lazy: keeps utils_webview import-clean if httpx absent

    async def stream_to_webview(session_id: str, agent_id: str, chunk: str, step: int) -> None:
        endpoint = f"{webview_base}/api/webview/sessions/{session_id}/stream"
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    endpoint,
                    json={"chunk": chunk, "agent_id": agent_id, "step": step},
                    timeout=timeout_seconds,
                )
        except httpx.TimeoutException:
            pass
        except Exception as e:
            logger.debug(f"WebView streaming error: {e}")

    return stream_to_webview