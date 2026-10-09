"""Egress allowlist proxy for the sandbox (073 W8, ``CODE_EXEC_NETWORK=proxy``).

A dev sandbox needs package registries, not the open internet. Under ``proxy`` the
session container sits on an ``--internal`` docker network (no route out) next to
ONE sidecar container that runs THIS file: an HTTP proxy that forwards only to the
hosts on ``CODE_EXEC_EGRESS_ALLOW``. The sandbox gets ``HTTP(S)_PROXY`` pointing at
the sidecar; anything that ignores the proxy simply has no route.

- ``CONNECT host:port`` (HTTPS, git, pip, npm): the tunnel opens only for an allowed
  host and a port in ``{443, 80}`` (+ ``CODE_EXEC_EGRESS_PORTS``);
- plain ``GET http://host/...``: forwarded only for an allowed host;
- everything else: ``403``. No DNS answer for a refused host is ever fetched.
- Host match: exact, or a ``*.suffix`` / ``.suffix`` wildcard entry.

Stdlib only, single file: the sidecar runs it with the sandbox image's own
``python3`` (``python3 egress_proxy.py <port> <comma allowlist>``), and the agent
process imports :func:`host_allowed` / :func:`parse_allowlist` for its own checks.
No ``from __future__``-sensitive closures here.
"""
import asyncio
import os
import sys
from typing import FrozenSet, Iterable, Optional, Tuple

DEFAULT_ALLOW = (
    "pypi.org", "files.pythonhosted.org",
    "registry.npmjs.org", "registry.yarnpkg.com",
    "github.com", "codeload.github.com", "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "crates.io", "static.crates.io", "index.crates.io",
    "proxy.golang.org", "sum.golang.org",
    "rubygems.org", "repo.maven.apache.org", "deb.debian.org",
)
DEFAULT_PORTS = (443, 80)
_MAX_HEADER = 64 * 1024


def parse_allowlist(raw: Optional[str]) -> Tuple[str, ...]:
    """``CODE_EXEC_EGRESS_ALLOW`` -> host patterns. Unset -> the registry default;
    ``+a,b`` appends to the default; anything else replaces it."""
    if raw is None or not str(raw).strip():
        return DEFAULT_ALLOW
    text = str(raw).strip()
    extra = text.startswith("+")
    items = tuple(h.strip().lower() for h in text.lstrip("+").split(",") if h.strip())
    return tuple(dict.fromkeys((DEFAULT_ALLOW + items) if extra else items))


def parse_ports(raw: Optional[str]) -> FrozenSet[int]:
    ports = set(DEFAULT_PORTS)
    for p in (raw or "").split(","):
        p = p.strip()
        if p.isdigit() and 0 < int(p) < 65536:
            ports.add(int(p))
    return frozenset(ports)


def host_allowed(host: str, allow: Iterable[str]) -> bool:
    h = (host or "").strip().strip("[]").rstrip(".").lower()
    if not h:
        return False
    for pat in allow:
        pat = pat.strip().lower()
        if not pat:
            continue
        if pat.startswith("*."):
            pat = pat[1:]
        if pat.startswith("."):
            if h.endswith(pat) or h == pat[1:]:
                return True
        elif h == pat:
            return True
    return False


def _split_hostport(target: str, default_port: int) -> Tuple[str, int]:
    target = target.strip()
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = target.rpartition(":") if target.count(":") == 1 else (target, "", "")
    try:
        return host, int(port) if port else default_port
    except ValueError:
        return host, -1


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


class EgressProxy:
    def __init__(self, allow: Iterable[str], ports: Iterable[int] = DEFAULT_PORTS):
        self.allow = tuple(allow)
        self.ports = frozenset(ports)
        self.refused = 0

    async def _refuse(self, writer, why: str) -> None:
        self.refused += 1
        body = f"egress refused by the sandbox proxy: {why}\n".encode()
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                     b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        try:
            await writer.drain()
        finally:
            writer.close()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except Exception:
            writer.close()
            return
        if len(head) > _MAX_HEADER:
            return await self._refuse(writer, "header too large")
        line, _, rest = head.decode("latin-1").partition("\r\n")
        parts = line.split()
        if len(parts) != 3:
            return await self._refuse(writer, "bad request line")
        method, target, version = parts
        if method.upper() == "CONNECT":
            host, port = _split_hostport(target, 443)
            if port not in self.ports:
                return await self._refuse(writer, f"port {port} not allowed")
            if not host_allowed(host, self.allow):
                return await self._refuse(writer, f"host {host} not on the allowlist")
            try:
                up_r, up_w = await asyncio.open_connection(host, port)
            except Exception as e:
                writer.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{e}".encode())
                writer.close()
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
            return
        if not target.lower().startswith("http://"):
            return await self._refuse(writer, "only CONNECT and absolute http:// requests")
        hostport, _, path = target[7:].partition("/")
        host, port = _split_hostport(hostport, 80)
        if port not in self.ports:
            return await self._refuse(writer, f"port {port} not allowed")
        if not host_allowed(host, self.allow):
            return await self._refuse(writer, f"host {host} not on the allowlist")
        try:
            up_r, up_w = await asyncio.open_connection(host, port)
        except Exception as e:
            writer.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{e}".encode())
            writer.close()
            return
        headers = [h for h in rest.split("\r\n") if h and not h.lower().startswith("proxy-")]
        up_w.write(f"{method} /{path} {version}\r\n".encode("latin-1")
                   + "\r\n".join(headers).encode("latin-1") + b"\r\n\r\n")
        await up_w.drain()
        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))

    async def serve(self, host: str, port: int):
        return await asyncio.start_server(self.handle, host, port)


def main(argv=None) -> int:  # pragma: no cover - runs inside the sidecar container
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(argv[0]) if argv else 3128
    allow = parse_allowlist(argv[1] if len(argv) > 1 else os.environ.get("CODE_EXEC_EGRESS_ALLOW"))
    ports = parse_ports(argv[2] if len(argv) > 2 else os.environ.get("CODE_EXEC_EGRESS_PORTS"))

    async def _run():
        server = await EgressProxy(allow, ports).serve("0.0.0.0", port)
        async with server:
            await server.serve_forever()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
