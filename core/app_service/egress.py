"""032 — per-app egress rules.

``--network none`` cannot publish a port and an ``--internal`` network cannot
either, so "no network" is NOT the mechanism. Every app gets its own bridge and
the supervisor owns ONE nft table per app (``inet polyrob_app_<slug>``), hooked
on ``forward``, that drops everything leaving the app's subnet except replies —
plus, for ``allowlist``, the resolved addresses. Rendered from a fixed template
with the subnet and the addresses as the only substitutions; ``open`` renders
nothing and removes the table.
"""
import asyncio
import ipaddress
import logging
import re
import socket
from typing import List, NamedTuple, Sequence, Tuple

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HOST_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
MODES = ("none", "allowlist", "open")

#: Cloud metadata services, named explicitly even though every one of them also
#: falls in a range below — an allow-host that resolves here is the classic
#: credential-theft target and must never get an ACCEPT rule.
METADATA_ADDRS = frozenset(
    ipaddress.ip_address(a) for a in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200"))

#: CGNAT (RFC 6598) — not ``is_private`` on every Python, so it is named.
_EXTRA_DENY_NETS = tuple(ipaddress.ip_network(n) for n in ("100.64.0.0/10",))


def is_public_addr(addr) -> bool:
    """Only a globally routable unicast address may be admitted out of an app
    sandbox. Loopback, RFC1918/ULA, link-local, CGNAT, multicast, reserved and
    unspecified addresses are refused in BOTH families — the allow-list is
    re-resolved every tick, so a name that later points at ``169.254.169.254``
    (DNS rebinding) must be refused at rule-render time, not at declare time."""
    try:
        ip = ipaddress.ip_address(str(addr))
    except ValueError:
        return False
    if ip in METADATA_ADDRS:
        return False
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    if any(ip in net for net in _EXTRA_DENY_NETS if net.version == ip.version):
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        return is_public_addr(ip.ipv4_mapped)
    return True


class Allowlist(NamedTuple):
    """Resolved allow-list. *refused* names the declared hostnames that produced
    NO admissible address, so the supervisor can tell the owner instead of
    silently rendering an empty allow set."""
    addrs: List[str]
    refused: List[str]


def table_name(slug: str) -> str:
    if not _SLUG_RE.fullmatch(str(slug)):
        raise ValueError(f"invalid slug {slug!r}")
    return "polyrob_app_" + slug.replace("-", "_")


def _validate_subnet(subnet: str) -> str:
    return str(ipaddress.ip_network(str(subnet), strict=False))


def render_rules(*, slug: str, subnet: str, mode: str, allow_addrs: Sequence[str]) -> str:
    """The nft script for one app; ``""`` for ``open``."""
    if mode not in MODES:
        raise ValueError(f"egress mode must be one of {', '.join(MODES)}")
    if mode == "open":
        return ""
    net = _validate_subnet(subnet)
    table = table_name(slug)
    # Validate first (junk REFUSES the render), then admit public addresses only —
    # defence in depth behind resolve_allowlist, which already filtered.
    addrs: List[str] = []
    if mode == "allowlist":
        validated = {str(ipaddress.ip_address(a)) for a in allow_addrs}
        addrs = sorted(a for a in validated if is_public_addr(a))
    lines = [
        f"table inet {table}",
        f"delete table inet {table}",
        f"table inet {table} {{",
        "    chain forward {",
        "        type filter hook forward priority -10; policy accept;",
        f"        ip saddr {net} ct state established,related accept",
    ]
    if addrs:
        v4 = [a for a in addrs if ":" not in a]
        v6 = [a for a in addrs if ":" in a]
        if v4:
            lines.append(f"        ip saddr {net} ip daddr {{ {', '.join(v4)} }} accept")
        if v6:
            lines.append(f"        ip saddr {net} ip6 daddr {{ {', '.join(v6)} }} accept")
    lines += [
        f"        ip saddr {net} drop",
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)


async def resolve_allowlist(hostnames: Sequence[str], resolver=None) -> Allowlist:
    """Resolve the declared hostnames to PUBLIC addresses (both families),
    de-duplicated.

    *resolver(host) -> list[str]* is injectable; the default runs
    ``socket.getaddrinfo`` off the loop. Unresolvable names are skipped (the rule
    set then simply does not admit them — deny by construction), and every
    resolved address is filtered through :func:`is_public_addr`, so a declared
    name that points at loopback, an RFC1918 host or a metadata service never
    earns an ACCEPT rule out of the sandbox. A hostname left with no admissible
    address is REPORTED (``Allowlist.refused`` + a warning), never silently
    dropped into an empty allow set."""
    out: List[str] = []
    refused: List[str] = []
    for host in hostnames or []:
        h = str(host).strip().lower()
        if not _HOST_RE.fullmatch(h):
            refused.append(f"{h[:80]} (not a hostname)")
            continue
        try:
            if resolver is not None:
                addrs = list(resolver(h))
            else:
                infos = await asyncio.to_thread(socket.getaddrinfo, h, None)
                addrs = [i[4][0] for i in infos]
        except (OSError, ValueError):
            refused.append(f"{h} (does not resolve)")
            continue
        admitted = 0
        non_public = 0
        for a in addrs:
            if not is_public_addr(a):
                non_public += 1
                continue
            s = str(ipaddress.ip_address(str(a)))
            admitted += 1
            if s not in out:
                out.append(s)
        if admitted == 0:
            why = "resolves only to a private/link-local address" if non_public else "does not resolve"
            refused.append(f"{h} ({why})")
    if refused:
        logger.warning("app egress allowlist refused %s", ", ".join(refused))
    return Allowlist(out, refused)


class EgressApplier:
    """Apply/remove one app's table through *sys_runner*."""

    def __init__(self, sys_runner):
        self._run = sys_runner

    async def apply(self, *, slug: str, subnet: str, mode: str,
                    allow_addrs: Sequence[str]) -> Tuple[bool, str]:
        script = render_rules(slug=slug, subnet=subnet, mode=mode, allow_addrs=allow_addrs)
        if not script:
            return await self.remove(slug)
        code, _out, err = await self._run(["nft", "-f", "-"], input=script, timeout=30)
        if code != 0:
            return False, (err or "nft -f failed").strip()[:400]
        return True, ""

    async def table_present(self, slug: str) -> bool:
        """Is this app's table loaded in the kernel RIGHT NOW? nft rules are
        ephemeral kernel state — a reboot or a firewall flush drops them while
        the container comes back on ``--restart unless-stopped``, so the
        supervisor asks every tick before deciding to re-apply."""
        table = table_name(slug)
        try:
            code, _out, _err = await self._run(
                ["nft", "list", "table", "inet", table], timeout=30)
        except Exception:
            return False
        return code == 0

    async def remove(self, slug: str) -> Tuple[bool, str]:
        table = table_name(slug)
        code, _out, err = await self._run(["nft", "delete", "table", "inet", table], timeout=30)
        # A missing table is not an error: the desired state (no rules) holds.
        if code != 0 and "No such file" not in (err or "") and "does not exist" not in (err or ""):
            return False, (err or "nft delete failed").strip()[:400]
        return True, ""
