"""The ONE answer to "can this process drive a browser, and through what?"

A wallet-custody process (``core.security.host_execution.wallet_custody_enabled``)
refuses to launch Chromium beside the signer; it may only CONNECT to a
separately isolated browser (``BROWSER_CDP_URL`` / ``BROWSER_WSS_URL``). Every
seat that speaks about the browser rail — the launch refusal, the step loop's
page-state observation, the ``<tool-catalog>``, ``polyrob doctor`` and the
status snapshot — reads this module, so no seat can say "no remote browser is
configured" while one is configured and running (the 2026-09-17 prod
misdiagnosis: the endpoint was set, the service was up, and the manager
dropped the setting on the floor).

Three states, one sentence each:

- ``unset``       — custody, no endpoint configured. Remedy: install the service.
- ``unreachable`` — an endpoint is configured but the probe failed. Remedy: check
                    the service. The reason is a CLASS (``connection refused``),
                    never the URL, which may carry a token.
- ``ok``          — local launch allowed, or the endpoint answered.
- ``configured``  — an endpoint is configured and was NOT probed (a status seat
                    asked with ``probe=False`` for a non-loopback endpoint; the
                    status snapshot does no network read by default).

Reads are cheap: a 2 s probe, memoised for ``PROBE_TTL_SEC``. Endpoint values are
never rendered.
"""
from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from core.security.host_execution import wallet_custody_enabled

PROBE_TTL_SEC = 60.0
PROBE_TIMEOUT_SEC = 2.0

INSTALL_REMEDY = "ask the owner to run `polyrob browser install`"
SERVICE_REMEDY = "ask the owner to check `polyrob-browser.service` (`polyrob browser status`)"


@dataclass(frozen=True)
class BrowserRailStatus:
    state: str                 # "ok" | "unset" | "unreachable" | "configured"
    endpoint: str              # "local" | "cdp" | "wss"
    custody: bool
    reason: str = ""           # unreachable only: a reason CLASS, never a URL
    version: str = ""          # ok+remote: the browser's reported version, if any

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    @property
    def usable(self) -> bool:
        """Whether a call site should TRY the rail (an unprobed endpoint is tried)."""
        return self.state in ("ok", "configured")

    @property
    def remedy(self) -> str:
        if self.state == "unset":
            return INSTALL_REMEDY
        if self.state == "unreachable":
            return SERVICE_REMEDY
        return ""

    def refusal(self) -> str:
        """The sentence a refusing call site raises. Empty when ``ok``."""
        if self.state == "unset":
            return ("Local Chromium is unavailable in a custody process and no remote "
                    "browser endpoint is configured (BROWSER_CDP_URL / BROWSER_WSS_URL) — "
                    f"{self.remedy}.")
        if self.state == "unreachable":
            return (f"The remote browser endpoint ({self.endpoint}) is configured but not "
                    f"reachable ({self.reason or 'probe failed'}) — {self.remedy}.")
        return ""

    def line(self) -> str:
        """The one-line rendering every status seat prints."""
        if self.state == "ok" and self.endpoint == "local":
            return "local Chromium"
        if self.state == "ok":
            ver = f" ({self.version})" if self.version else ""
            return f"remote {self.endpoint} ok{ver}"
        if self.state == "unset":
            return f"none (custody) → {self.remedy}"
        if self.state == "configured":
            return f"remote {self.endpoint} configured (not probed)"
        return f"remote {self.endpoint} configured, unreachable ({self.reason or 'probe failed'}) → {self.remedy}"


def configured_endpoint() -> str:
    """``"cdp"`` / ``"wss"`` / ``"local"`` from the environment alone (no probe)."""
    if os.environ.get("BROWSER_CDP_URL"):
        return "cdp"
    if os.environ.get("BROWSER_WSS_URL"):
        return "wss"
    return "local"


def _probe_cdp(url: str) -> tuple[bool, str, str]:
    """GET <url>/json/version. Returns (ok, reason_class, version)."""
    import json
    import urllib.error
    import urllib.request
    base = url.rstrip("/")
    if base.startswith("ws://"):
        base = "http://" + base[len("ws://"):]
    elif base.startswith("wss://"):
        base = "https://" + base[len("wss://"):]
    try:
        with urllib.request.urlopen(base + "/json/version", timeout=PROBE_TIMEOUT_SEC) as resp:
            body = resp.read(4096)
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}", ""
    except urllib.error.URLError as exc:
        return False, _reason_class(exc.reason), ""
    except (OSError, ValueError) as exc:
        return False, _reason_class(exc), ""
    try:
        version = str(json.loads(body.decode("utf-8", "replace")).get("Browser", ""))
    except Exception:
        version = ""
    return True, "", version


def _probe_tcp(url: str) -> tuple[bool, str, str]:
    """A Playwright server has no unauthenticated GET; prove the socket only."""
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme in ("wss", "https") else 80)
    if not host:
        return False, "malformed endpoint", ""
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SEC):
            return True, "", ""
    except OSError as exc:
        return False, _reason_class(exc), ""


def _reason_class(exc) -> str:
    """A class of failure the owner can act on; never the message (it may echo the URL)."""
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "dns failure"
    name = type(exc).__name__
    if name in ("SSLError", "SSLCertVerificationError"):
        return "tls failure"
    return name or "probe failed"


_cache: dict = {"at": 0.0, "value": None, "key": None}


def _env_key() -> tuple:
    """The inputs the answer depends on; a changed env invalidates the cache."""
    return (wallet_custody_enabled(),
            os.environ.get("BROWSER_CDP_URL") or "",
            os.environ.get("BROWSER_WSS_URL") or "")


def browser_rail_status(*, refresh: bool = False, probe: bool = True) -> BrowserRailStatus:
    """The memoised answer.

    ``refresh=True`` bypasses the TTL (doctor, install). ``probe=False`` never
    opens a socket to a NON-loopback endpoint: it returns the fresh cache if
    there is one, else ``configured`` (unprobed). A loopback endpoint is always
    probed — that is a local read, not a network one.
    """
    now = time.monotonic()
    key = _env_key()
    cached: Optional[BrowserRailStatus] = _cache["value"]
    if (cached is not None and not refresh and _cache["key"] == key
            and now - _cache["at"] < PROBE_TTL_SEC):
        return cached
    status = _compute(probe=probe)
    if status.state != "configured":
        _cache["at"] = now
        _cache["value"] = status
        _cache["key"] = key
    return status


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["value"] = None
    _cache["key"] = None


def _is_loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "").strip("[]").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def _compute(*, probe: bool = True) -> BrowserRailStatus:
    custody = wallet_custody_enabled()
    endpoint = configured_endpoint()
    if endpoint == "local":
        if custody:
            return BrowserRailStatus("unset", "local", True)
        return BrowserRailStatus("ok", "local", False)
    url = os.environ.get("BROWSER_CDP_URL") if endpoint == "cdp" else os.environ.get("BROWSER_WSS_URL")
    if not probe and not _is_loopback(url or ""):
        return BrowserRailStatus("configured", endpoint, custody)
    ok, reason, version = (_probe_cdp if endpoint == "cdp" else _probe_tcp)(url or "")
    if ok:
        return BrowserRailStatus("ok", endpoint, custody, version=version)
    return BrowserRailStatus("unreachable", endpoint, custody, reason=reason)
