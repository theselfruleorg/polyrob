"""Bounded, anonymous HTTP status probes with connect-time SSRF protection."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

from core.security.url_policy import get_url_validator


def origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(ord(c) < 33 for c in url)):
        raise ValueError("expected an anonymous http(s) URL without control characters")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


async def http_status(url: str, timeout: float, *, allowed_loopback_origins=()) -> int:
    """GET headers only; no credentials/proxies/cookies, bounded redirects/deadline.

    Loopback grants must come from trusted run context, never from check payloads.
    A grant covers exactly one origin with a literal loopback IP (not DNS aliases).
    Every redirect is revalidated and every connection pins the checked address.
    """
    import aiohttp

    grants = {origin(item) for item in allowed_loopback_origins}

    class PinnedResolver(aiohttp.abc.AbstractResolver):
        def __init__(self, hostname, address):
            self.hostname, self.address = hostname, address

        async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
            if host.lower() != self.hostname:
                raise OSError("unexpected resolver host")
            address_family = socket.AF_INET6 if ":" in self.address else socket.AF_INET
            return [{"hostname": host, "host": self.address, "port": port,
                     "family": address_family, "proto": 0, "flags": socket.AI_NUMERICHOST}]

        async def close(self):
            pass

    async def probe():
        current = url
        for hop in range(6):
            target = origin(current)
            try:
                literal = ipaddress.ip_address(target[1])
            except ValueError:
                literal = None
            if target in grants and literal is not None and literal.is_loopback:
                pinned = str(literal)
            else:
                valid, reason, pinned = await asyncio.to_thread(
                    get_url_validator(allow_http=True).validate_and_resolve, current)
                if not valid or not pinned:
                    raise ValueError(f"HTTP probe refused: {reason}")
            connector = aiohttp.TCPConnector(resolver=PinnedResolver(target[1], pinned),
                                              use_dns_cache=False, force_close=True)
            async with aiohttp.ClientSession(
                    connector=connector, trust_env=False, auto_decompress=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(current, allow_redirects=False,
                                       headers={"User-Agent": "polyrob-acceptance-check"}) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or hop == 5:
                            raise ValueError("HTTP redirect missing target or limit exceeded")
                        current = urljoin(current, location)
                        continue
                    return response.status
        raise ValueError("HTTP redirect limit exceeded")

    return await asyncio.wait_for(probe(), timeout=timeout)
