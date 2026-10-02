"""The ONE pinned DNS resolver for SSRF-validated outbound connections.

Validating a hostname and then letting the HTTP client resolve it again leaves a
DNS-rebinding window. Every caller validates once
(``url_policy.MCPURLValidator.validate_and_resolve``) and then hands the client
this resolver, which answers for exactly that host with exactly the validated IP.

⚠️ The host the client asks about is the WIRE form. aiohttp resolves
``yarl.URL(url).raw_host`` — the IDNA/punycode form, lowercased — while
``urlparse(url).hostname`` is Unicode. Comparing the two let an IDN host fall
through to a fresh lookup that an attacker's DNS could answer with
``169.254.169.254`` (H14, 2026-09-23). Both sides are therefore normalised with
``host_key`` and ANY mismatch raises ``OSError``: this resolver never performs a
lookup of its own.

Duck-typed on purpose (``resolve`` + ``close``): aiohttp only calls those two, and
core stays importable without aiohttp.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Optional


def host_key(host: Optional[str]) -> str:
    """Normalise a host to the form a client puts on the wire.

    Lowercased, IPv6 brackets and one trailing dot removed, and a non-ASCII
    name IDNA-encoded the same way yarl (and so aiohttp) encodes it. An empty
    or unencodable host returns ``""``, which matches nothing.
    """
    h = str(host or "").strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.rstrip(".").lower()
    if not h:
        return ""
    if not h.isascii():
        try:
            import yarl
            raw = yarl.URL.build(scheme="http", host=h).raw_host or ""
        except Exception:
            try:
                raw = h.encode("idna").decode("ascii")
            except Exception:
                return ""
        h = raw.rstrip(".").lower()
    return h


def url_host_key(url: str) -> str:
    """``host_key`` of the host in ``url``, taken the way aiohttp takes it."""
    try:
        import yarl
        return host_key(yarl.URL(url).raw_host)
    except Exception:
        from urllib.parse import urlparse
        try:
            return host_key(urlparse(url).hostname)
        except Exception:
            return ""


def is_ip_literal(host: Optional[str]) -> bool:
    try:
        ipaddress.ip_address(host_key(host) or "-")
        return True
    except ValueError:
        return False


class PinnedResolver:
    """Resolve ONE host to ONE pre-validated IP; refuse every other host."""

    def __init__(self, host: str, ip: str, family: Optional[int] = None):
        self._host = host_key(host)
        self._ip = str(ip)
        if family is None:
            family = socket.AF_INET6 if ":" in self._ip else socket.AF_INET
        self._family = family
        if not self._host or not self._ip:
            raise ValueError("a pinned resolver needs a host and an IP")

    @classmethod
    def for_url(cls, url: str, ip: str, family: Optional[int] = None) -> "PinnedResolver":
        return cls(url_host_key(url), ip, family)

    @property
    def host(self) -> str:
        return self._host

    @property
    def ip(self) -> str:
        return self._ip

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host_key(host) != self._host:
            # A redirect, a second origin or an encoding we did not expect.
            # Never fall through to a real lookup: that is the rebinding hole.
            raise OSError(f"refusing to resolve non-pinned host: {host!r}")
        return [{
            "hostname": host,
            "host": self._ip,
            "port": port,
            "family": self._family,
            "proto": 0,
            "flags": socket.AI_NUMERICHOST,
        }]

    async def close(self) -> None:
        return None
