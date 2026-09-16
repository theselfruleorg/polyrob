"""Socket.IO CORS for the console: the allowlist, and what counts as same-origin.

Extracted from `server.py` (god-file ratchet) as one concern — "which browser
origins may open a socket to this console?" — beside `webgate.csrf_guard`, which
answers the same question for HTTP mutations and leans on the same fact:

⚠️ `Host` is a browser-FORBIDDEN request header. A cross-origin attacker page
cannot make `Origin` match it, which is why comparing the two is a real check and
not a formality. `X-Forwarded-Host`/`-Proto` ARE settable from cross-origin JS and
must never feed that comparison — only the scheme may vary, which concedes
nothing a network MITM does not already have.
"""
import os
from typing import Callable, List, Optional

#: Legacy default front-end host (one env var, not two independent hardcodes).
DEFAULT_WEBVIEW_DOMAIN = "localhost:3000"


def webview_domain() -> str:
    return os.environ.get("WEBVIEW_DOMAIN", DEFAULT_WEBVIEW_DOMAIN).strip()


def compute_cors_origins(bind_port: int) -> List[str]:
    """Explicit ``CORS_ALLOW_ORIGINS`` wins verbatim; otherwise the default list:
    legacy localhost:3000 entries + ``WEBVIEW_DOMAIN`` + the console's own serving
    origins (bind port on localhost/127.0.0.1) — the webview serves its own UI, so
    the serving origin must be allowed or the browser's same-origin Socket.IO
    handshake is rejected with a 400 (P0-1, 2026-07-06)."""
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    domain = webview_domain()
    origins = [
        "http://localhost:3000",
        "https://localhost:3000",
        f"https://{domain}",
        f"http://{domain}",
    ]
    for scheme in ("http", "https"):
        for host in ("localhost", "127.0.0.1"):
            origins.append(f"{scheme}://{host}:{bind_port}")
    return list(dict.fromkeys(origins))


def make_origin_allowed(origins: List[str]) -> Callable:
    """The engineio ``cors_allowed_origins`` callable over *origins*: allow the
    explicit list OR a TRUE same-origin request (``Origin == scheme://Host``)."""
    def _allowed(origin: Optional[str], environ: Optional[dict] = None) -> bool:
        if origin in origins:
            return True
        host = (environ or {}).get("HTTP_HOST")
        if not origin or not host:
            return False
        return origin in (f"http://{host}", f"https://{host}")
    return _allowed


__all__ = ["DEFAULT_WEBVIEW_DOMAIN", "webview_domain", "compute_cors_origins",
           "make_origin_allowed"]
