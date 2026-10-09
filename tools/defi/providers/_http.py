"""The two helpers every keyless provider carried by hand.

``get_json`` is one pooled GET with the project user-agent and a bounded
timeout; ``parse_int`` is the "a malformed number is None (unknown), never
0" rule — a zero would read as a free bridge, a free trade or a zero floor.
"""
from __future__ import annotations

import threading
import json
from typing import Any, Optional

USER_AGENT = "polyrob-defi/1.0"

#: ⚠️ ONE pooled client for every provider, because on this box a NEW connection
#: costs ~6 seconds (measured 2026-09-24). Outbound IPv6 does not work, yet the
#: host advertises a global IPv6 address and an IPv6 default route, so resolvers
#: hand out AAAA records and httpx/httpcore connect to the first resolved address
#: rather than racing families the way curl does — each new connection burns the
#: IPv6 timeout (3.07 s, measured) and then falls back to IPv4 (0.00 s).
#:
#:     httpx.get (fresh client):  6.24s      reused client, call 1:  6.52s
#:     httpx.get (fresh client):  6.22s      reused client, call 2:  0.01s
#:                                           reused client, call 3:  0.01s
#:
#: `dexscreener._get` built a client per call and so paid that on EVERY price
#: lookup: 6.33 s mean, 0.26 s spread between a 30-pool and a 1-pool token. Over
#: 84 holdings that is 532 s — the 556-563 s that killed four SAFETY runs.
#: Pooling makes it one handshake plus ~0.01 s a call.
#:
#: Thread-safe by design: `httpx.Client` is, and the lazy init below is guarded,
#: because the `defi_data` verbs now run on worker threads via `to_thread`.
_CLIENT: Optional[Any] = None
_CLIENT_LOCK = threading.Lock()

#: Bounded on purpose. A connect that never returns is a rail that never returns.
CONNECT_TIMEOUT_SEC = 8.0
READ_TIMEOUT_SEC = 12.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def client():
    """The shared pooled HTTP client. Never build your own — see the note above."""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:          # re-check inside the lock
                import httpx
                _CLIENT = httpx.Client(
                    timeout=httpx.Timeout(READ_TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC),
                    limits=httpx.Limits(max_keepalive_connections=20,
                                        max_connections=40,
                                        keepalive_expiry=120.0),
                    headers={"user-agent": USER_AGENT,
                             "accept": "application/json"},
                    follow_redirects=False,
                )
    return _CLIENT


class HttpStatusError(Exception):
    """A non-2xx answer from a provider.

    ⚠️ It carries ``.code`` ON PURPOSE. ``get_json`` used to be
    ``urllib.request.urlopen``, which RAISES ``HTTPError`` on 4xx/5xx and whose
    error exposes ``.code``; ``httpx`` does neither — it returns a response
    object, and its own ``HTTPStatusError`` hides the number under
    ``.response.status_code``. ``geckoterminal._is_rate_limited`` reads exactly
    ``.code`` to decide whether a 429 is worth one backoff and retry, so
    dropping the attribute would have turned a retryable rate-limit into
    "this chain has no pools" — a wrong answer rather than a slow one.
    """

    def __init__(self, url: str, code: int, reason: str = "") -> None:
        from core.security.redaction import redact_url
        super().__init__(f"HTTP Error {code} ({redact_url(url)})")
        self.url = redact_url(url)
        self.code = code
        self.reason = reason


def get_json(url: str, *, timeout: float, user_agent: str = USER_AGENT) -> Any:
    """One GET on the SHARED pool — never a fresh connection (see the note above).

    ``timeout`` is per call because the callers own different budgets: a
    bridge-status read and a pool listing are not the same kind of wait.
    """
    return request_json("GET", url, timeout=timeout, user_agent=user_agent)


def request_json(method: str, url: str, *, timeout: float, payload=None,
                 user_agent: str = USER_AGENT) -> Any:
    """Bounded decoded JSON over the shared pool; redirects never propagate keys."""
    kwargs = {"json": payload} if payload is not None else {}
    with client().stream(method, url, timeout=timeout, follow_redirects=False,
                         headers={"accept": "application/json",
                                  "user-agent": user_agent}, **kwargs) as resp:
        if not 200 <= resp.status_code < 300:
            raise HttpStatusError(url, resp.status_code,
                                  getattr(resp, "reason_phrase", ""))
        body = bytearray()
        for chunk in resp.iter_bytes(chunk_size=65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("provider response exceeds the size limit")
        return json.loads(body)


def parse_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Provider numbers arrive as decimal STRINGS (or ints). A malformed one
    is *default* (None = unknown), never 0."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
