"""Narrow loopback-port allowlist for the agent's own published sandbox servers (WS-4).

At AGENT_COMPUTE_POSTURE>=1 the persistent sandbox container publishes its ports to
host loopback (``127.0.0.1:<hostport>``). To let the agent HTTP-test a server it
started — WITHOUT opening a blanket ``BROWSER_ALLOW_PRIVATE_URLS`` hole — the SSRF
guards (browser + web_fetch) consult this registry: a URL is permitted ONLY when its
host is loopback AND its port is one the sandbox actually published for this process.

Security properties:
- ONLY loopback hosts (127.0.0.0/8, ::1, ``localhost``) can match — cloud metadata
  (169.254.169.254, link-local) and RFC1918 are NOT loopback and can never be allowed
  here;
- ONLY http(s);
- empty by default — nothing is allowed until the pool publishes a port. The set is
  process-global (populated by ``tools/shell/backend_pool`` on container setup).
"""
from __future__ import annotations

import ipaddress
import threading
from typing import Iterable, Set
from urllib.parse import urlparse

_ALLOWED_PORTS: Set[int] = set()
_LOCK = threading.Lock()

_LOOPBACK_NAMES = {"localhost"}


def allow_loopback_ports(ports: Iterable[int]) -> None:
    """Register host loopback ports the SSRF guards may reach (additive)."""
    with _LOCK:
        for p in ports:
            try:
                _ALLOWED_PORTS.add(int(p))
            except (TypeError, ValueError):
                continue


def revoke_loopback_ports(ports: Iterable[int]) -> None:
    """Remove specific host ports from the allowlist (on session teardown), so a
    stale ephemeral port a later unrelated bind reuses is not silently reachable."""
    with _LOCK:
        for p in ports:
            try:
                _ALLOWED_PORTS.discard(int(p))
            except (TypeError, ValueError):
                continue


def clear_loopback_ports() -> None:
    with _LOCK:
        _ALLOWED_PORTS.clear()


def _is_loopback_host(host: str) -> bool:
    if not host:
        return False
    h = host.strip().strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def is_loopback_allowed(url: str) -> bool:
    """True iff ``url`` is http(s) to a loopback host on a currently-published port."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not _is_loopback_host(parsed.hostname or ""):
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is None:
        port = 80 if parsed.scheme == "http" else 443
    with _LOCK:
        return int(port) in _ALLOWED_PORTS
