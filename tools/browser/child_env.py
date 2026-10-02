"""Child-process environment policy for Chromium / the Playwright CLI (S2).

``tools/browser/browser.py`` used to build its launch environment with
``os.environ.copy()`` and hand the result to ``playwright.chromium.launch(env=…)``
— so every browser process the agent drove inherited the WHOLE agent
environment, ``AGENT_WALLET_MASTER_SEED`` and every provider API key included.
Chromium is the one child that renders attacker-controlled content, so a
renderer-side read of ``/proc/self/environ`` was a direct path from a hostile
page to the treasury seed. The Xvfb and raw-Chrome ``Popen`` calls and the
``python -m playwright install`` runs inherited the same thing implicitly, by
passing no ``env=`` at all.

Same rule as :mod:`tools.mcp.child_env`: **ambient inheritance is denied by
default**. The difference is only in WHICH host vars a browser genuinely needs,
so this delegates the scrub to the ONE policy
(:func:`tools.code_exec.env_policy.build_child_env`) and only widens the
*inherit* list. Widening the inherit list never widens the secret list — the
policy's secret-name filter runs last over everything, so a var named here that
happened to match ``*_API_KEY``/``*_SEED``/``*_TOKEN``/… is still dropped.
"""
from __future__ import annotations

from typing import Dict, Optional

#: Host env vars Chromium / Xvfb / the Playwright CLI genuinely need on top of
#: the base :data:`tools.code_exec.env_policy.SAFE_ALLOWLIST`. None of these can
#: hold a POLYROB credential.
BROWSER_ENV_ALLOWLIST = frozenset({
    # Locale / identity beyond the base list
    "LANGUAGE", "LOGNAME", "HOSTNAME",
    # X11 / Wayland / session bus — headed mode and Xvfb do not start without these
    "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "DBUS_SESSION_BUS_ADDRESS",
    # Where browsers / fonts / caches live
    "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "FONTCONFIG_PATH", "FONTCONFIG_FILE",
    "LD_LIBRARY_PATH", "LD_PRELOAD_PATH",
    # Interpreter context for `python -m playwright …`
    "VIRTUAL_ENV", "PYTHONHOME",
    # Proxy settings. A proxy URL CAN embed credentials, but a box behind a
    # proxy cannot download or drive a browser without them, and the process
    # receiving them is our own binary — not agent-authored code.
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    # Windows — the Playwright CLI does not start without these
    "SystemRoot", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
    "PROGRAMDATA", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
    "NUMBER_OF_PROCESSORS", "OS", "WINDIR",
})

#: Name PREFIXES that are browser configuration by construction.
#: ``PLAYWRIGHT_BROWSERS_PATH`` is the one prod actually depends on; ``PW_`` is
#: the driver's own namespace; ``CHROME_``/``CHROMIUM_`` carry executable and
#: sandbox paths.
BROWSER_ENV_PREFIXES = ("PLAYWRIGHT_", "PW_", "CHROME_", "CHROMIUM_")


def build_browser_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Scrubbed environment for a Chromium / Xvfb / Playwright-CLI child.

    ``extra`` is the caller-supplied overlay (e.g. a ``DISPLAY`` this process
    just discovered). Secret-named keys in ``extra`` are dropped by the shared
    policy, exactly as for a code-exec child.
    """
    # Lazy: keeps the code_exec package out of the browser/CLI import graph.
    from tools.code_exec.env_policy import build_child_env
    return build_child_env(
        extra or {},
        extra_allowlist=BROWSER_ENV_ALLOWLIST,
        allow_prefixes=BROWSER_ENV_PREFIXES,
    )


#: Host vars the Playwright NODE DRIVER needs on top of the browser list: a box
#: behind a TLS-intercepting proxy cannot reach anything without its CA bundle,
#: and ``DEBUG=pw:*`` is how the driver is traced. None can hold a credential.
DRIVER_ENV_ALLOWLIST = frozenset({
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "DEBUG", "DEBUG_FILE",
})


def build_driver_env(original: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """066 P0.3: scrubbed environment for the Playwright Node driver.

    ``original`` is what Playwright's own ``get_driver_env()`` built — a FULL
    ``os.environ`` copy plus its ``PW_*`` markers (``PW_LANG_NAME`` …). Only
    those markers are taken from it; everything else comes through the ONE
    policy (:func:`tools.code_exec.env_policy.build_child_env`), which drops
    every secret-named var last.
    """
    from tools.code_exec.env_policy import build_child_env
    markers = {k: v for k, v in (original or {}).items() if k.startswith("PW_")}
    return build_child_env(
        markers,
        extra_allowlist=BROWSER_ENV_ALLOWLIST | DRIVER_ENV_ALLOWLIST,
        allow_prefixes=BROWSER_ENV_PREFIXES,
    )


def install_driver_env_scrub() -> bool:
    """Make every Playwright driver this process starts get :func:`build_driver_env`.

    ``async_playwright().start()`` spawns ``playwright/driver/node`` with
    ``get_driver_env()`` = ``os.environ.copy()`` (prod 2026-09-23: the driver
    held the seed and every API key). There is no ``env=`` parameter on
    ``start()``, and swapping ``os.environ`` around an ``await`` would starve
    every other coroutine of its keys — so the ONE function the transport calls
    synchronously, right before its ``create_subprocess_exec``, is wrapped.

    Idempotent. Returns False (and the caller logs it) when the installed
    Playwright no longer has that seam — the driver then inherits the process
    env, which after 066 P0.2 no longer holds the seed.
    """
    try:
        from playwright._impl import _transport
    except ImportError:
        return False
    original = getattr(_transport, "get_driver_env", None)
    if original is None:
        return False
    if getattr(original, "_polyrob_scrubbed", False):
        return True

    def scrubbed_driver_env() -> Dict[str, str]:
        return build_driver_env(original())

    scrubbed_driver_env._polyrob_scrubbed = True  # type: ignore[attr-defined]
    _transport.get_driver_env = scrubbed_driver_env
    return True
