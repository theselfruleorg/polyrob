"""
MCP Security utilities.

URL validation for SSRF protection. The Fernet credential store (``MCPEncryption`` /
``get_encryption`` + key loading) lives in ``core/security/encryption.py`` since S6
(2026-08-29) and is re-exported here for this tier's callers.
"""

import os
import json
import socket
import ipaddress
import logging
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from core.security.encryption import (  # noqa: E402,F401  (re-exported for tools-tier callers + tests)
    _KEY_FILE_PATH,
    _is_production,
    _key_file_path,
    _load_or_generate_key,
    MCPEncryption,
    get_encryption,
)


class MCPURLValidator:
    """
    Validate MCP server URLs for security (SSRF protection).

    Blocks:
    - Non-HTTPS URLs
    - Localhost and loopback addresses
    - Private IP ranges (10.x, 172.16-31.x, 192.168.x)
    - Link-local addresses (169.254.x)
    - Cloud metadata endpoints
    """

    # Blocked hostnames
    BLOCKED_HOSTS = {
        'localhost',
        '127.0.0.1',
        '0.0.0.0',
        '::1',
        '[::1]',
        'metadata.google.internal',
        'metadata.goog',
        '169.254.169.254',  # AWS/GCP/Azure metadata
        'metadata.internal',
    }

    # Blocked IP networks (private/internal ranges). Belt-and-suspenders alongside the
    # property-based classification in _is_blocked_ip (which is the authoritative check).
    BLOCKED_NETWORKS = [
        ipaddress.ip_network('0.0.0.0/8'),        # "this network" (0.x routes to localhost)
        ipaddress.ip_network('10.0.0.0/8'),       # Private class A
        ipaddress.ip_network('172.16.0.0/12'),    # Private class B
        ipaddress.ip_network('192.168.0.0/16'),   # Private class C
        ipaddress.ip_network('127.0.0.0/8'),      # Loopback
        ipaddress.ip_network('169.254.0.0/16'),   # Link-local
        ipaddress.ip_network('100.64.0.0/10'),    # CGNAT / shared address space
        ipaddress.ip_network('::1/128'),          # IPv6 loopback
        ipaddress.ip_network('fc00::/7'),         # IPv6 private
        ipaddress.ip_network('fe80::/10'),        # IPv6 link-local
        ipaddress.ip_network('::ffff:0:0/96'),    # IPv4-mapped IPv6
    ]

    # RFC 6052 well-known NAT64 prefix: an IPv6-only/NAT64 resolver synthesizes
    # 64:ff9b::<embedded-ipv4> for a plain IPv4 literal/hostname (e.g.
    # 64:ff9b::5db8:d822 for 93.184.216.34). Unlike ipv4_mapped/sixtofour, the
    # stdlib `ipaddress` module has no property for this — decode it manually
    # so the embedded IPv4 gets evaluated against the SAME block rules (this is
    # not a bypass: a NAT64-wrapped 10.x address must still be blocked).
    _NAT64_PREFIX = ipaddress.ip_network('64:ff9b::/96')

    @staticmethod
    def _is_blocked_ip(ip_obj: "ipaddress._BaseAddress") -> bool:
        """Authoritative SSRF check by IP *property*, not a hand-maintained range list.

        Unmaps IPv4-mapped IPv6 (``::ffff:a.b.c.d``) and NAT64-synthesized
        addresses (``64:ff9b::a.b.c.d``) first — otherwise a v4 network check
        silently misses them — then blocks any non-global address class
        (private/loopback/link-local/reserved/multicast/unspecified) plus CGNAT.
        """
        if isinstance(ip_obj, ipaddress.IPv6Address):
            if ip_obj.ipv4_mapped is not None:
                ip_obj = ip_obj.ipv4_mapped
            elif getattr(ip_obj, "sixtofour", None) is not None:
                ip_obj = ip_obj.sixtofour
            elif ip_obj in MCPURLValidator._NAT64_PREFIX:
                ip_obj = ipaddress.IPv4Address(ip_obj.packed[-4:])
        if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
            return True
        # CGNAT is not is_private on every Python version — block explicitly.
        if ip_obj.version == 4 and ip_obj in ipaddress.ip_network('100.64.0.0/10'):
            return True
        return False

    def __init__(self, allow_http: bool = False):
        """
        Initialize validator.

        Args:
            allow_http: If True, allow HTTP URLs (for local development only)
        """
        self.allow_http = allow_http

    def validate(self, url: str) -> Tuple[bool, Optional[str]]:
        """
        Validate URL is safe for user MCP servers.

        Args:
            url: URL to validate

        Returns:
            Tuple of (is_valid, error_message)
            error_message is None if valid

        Note:
            This 2-tuple contract is depended upon by existing callers
            (``user_mcp_service.add_server``/``update_server``). For connect-time
            SSRF/DNS-rebinding protection, callers that need the resolved IP
            should use :meth:`validate_and_resolve`, which returns the validated
            pinned IP so the socket connects to the IP that was actually checked.
        """
        is_valid, error, _pinned_ip = self.validate_and_resolve(url)
        return is_valid, error

    def validate_and_resolve(
        self, url: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Validate URL AND return the validated, resolved IP to pin.

        Resolves the hostname exactly once and checks every resolved address
        against the blocked networks. On success it returns a single concrete
        IP that the caller should connect to directly (pinning), so a DNS
        rebind between validation and connection cannot redirect the socket to
        an internal/metadata address.

        Args:
            url: URL to validate

        Returns:
            Tuple of (is_valid, error_message, pinned_ip)
            - On success: (True, None, "<resolved public ip>")
            - On failure: (False, "<reason>", None)

        Security:
            The returned ``pinned_ip`` is the address that was validated. Callers
            MUST connect to this IP (preserving the original Host header / TLS
            SNI) rather than re-resolving DNS, otherwise the rebinding window
            reopens.
        """
        try:
            parsed = urlparse(url)

            # Check scheme
            if not self.allow_http and parsed.scheme != 'https':
                return False, "URL must use HTTPS", None

            if parsed.scheme not in ('http', 'https'):
                return False, f"Invalid scheme: {parsed.scheme}", None

            # Check hostname exists
            hostname = parsed.hostname
            if not hostname:
                return False, "Invalid URL: no hostname", None

            # Check for blocked hostnames
            hostname_lower = hostname.lower()
            if hostname_lower in self.BLOCKED_HOSTS:
                return False, f"Blocked host: {hostname}", None

            # Check for blocked hostname patterns
            if hostname_lower.endswith('.internal') or hostname_lower.endswith('.local'):
                return False, f"Internal hostname not allowed: {hostname}", None

            # Check port (optional - some MCP servers use non-standard ports)
            port = parsed.port
            if port is not None:
                # Block commonly abused ports
                if port in (22, 23, 25, 110, 143, 445, 3389):
                    return False, f"Port {port} is not allowed", None

            # Resolve hostname ONCE and check every IP. The first valid,
            # non-blocked address is returned as the pin target.
            pinned_ip: Optional[str] = None
            try:
                # Get all IPs for the hostname
                addresses = socket.getaddrinfo(hostname, port)
                for addr_info in addresses:
                    ip_str = addr_info[4][0]
                    try:
                        ip_obj = ipaddress.ip_address(ip_str)
                    except ValueError:
                        # Not a valid IP address, skip
                        continue

                    # Property-based classification (authoritative) — closes the whole
                    # class of internal addresses incl. IPv4-mapped IPv6, 0.0.0.0/8, CGNAT.
                    if self._is_blocked_ip(ip_obj):
                        return False, f"IP address in blocked range: {ip_str}", None
                    # Belt-and-suspenders: explicit range list too.
                    for network in self.BLOCKED_NETWORKS:
                        if ip_obj in network:
                            return False, f"IP address in blocked range: {ip_str}", None

                    # First clean address becomes the pin target
                    if pinned_ip is None:
                        pinned_ip = ip_str

            except socket.gaierror as e:
                return False, f"Cannot resolve hostname: {hostname} ({e})", None

            if pinned_ip is None:
                return False, f"Cannot resolve hostname to an IP: {hostname}", None

            return True, None, pinned_ip

        except Exception as e:
            return False, f"URL validation error: {e}", None

    def validate_server_type(self, server_type: str) -> Tuple[bool, Optional[str]]:
        """
        Validate server type is allowed for user MCP servers.

        STDIO is NOT allowed for user servers (security risk - executes local processes).

        Args:
            server_type: Server type to validate ('sse', 'http', 'streamable_http', 'stdio')

        Returns:
            Tuple of (is_valid, error_message)
        """
        allowed_types = ('sse', 'http', 'streamable_http')

        if server_type == 'stdio':
            return False, "STDIO server type is not allowed for user servers (security risk)"

        if server_type not in allowed_types:
            return False, f"Invalid server type: {server_type}. Must be one of: {', '.join(allowed_types)}"

        return True, None


def get_url_validator(allow_http: bool = False) -> MCPURLValidator:
    """
    Get URL validator instance.

    Args:
        allow_http: Allow HTTP URLs (development only)

    Returns:
        MCPURLValidator instance
    """
    return MCPURLValidator(allow_http=allow_http)
