"""Uvicorn launch path shared by ``polyrob serve`` and ``python main.py``.

This lives in the installed ``api`` package because repo-root ``main.py`` ships
in no wheel and console scripts never have the CWD on ``sys.path`` — importing
it from ``cli/commands/serve.py`` crashed every installed ``polyrob serve``
before its no-key preflight could even print (UX assessment 2026-08-07, B1).
``main.py`` stays the systemd entry point as a thin shim over this module.
"""

import os
from typing import Optional

# The ASGI app target uvicorn launches (factory function).
APP_TARGET = "api.app:get_app"


def _load_env():
    """Pre-load env files via the ONE layering SSOT (core.bootstrap.load_env).

    This used to be a fourth, divergent loader (ENV-only, single file
    ``config/.env.{ENV}`` XOR root ``.env``, no CONFIG_ENV, no layering). The
    app it launches runs ``core.bootstrap.load_env`` in its lifespan anyway,
    so a divergent pre-load could only disagree with what the app then sees;
    it exists at all so ``UVICORN_*``/``LOG_LEVEL`` below read file values.
    """
    from core.bootstrap import load_env

    resolved = load_env()
    print(f"Loaded environment (env: {resolved})")


def resolve_workers(workers=None) -> int:
    """The effective worker count: the explicit argument, else ``UVICORN_WORKERS``.

    A non-numeric ``UVICORN_WORKERS`` reads as 1 rather than crashing the
    launch with a bare ``ValueError`` — a typo in an env file must not take the
    server down with a traceback the operator has to decode.
    """
    if workers is not None:
        return int(workers)
    raw = (os.environ.get("UVICORN_WORKERS") or "").strip()
    if not raw:
        return 1
    try:
        return int(raw)
    except ValueError:
        print(f"UVICORN_WORKERS={raw!r} is not a number — using 1 worker")
        return 1


def multiworker_refusal(workers=None) -> Optional[str]:
    """Why ``workers`` may not start here, or ``None`` when it may (B13).

    N uvicorn workers = N autonomy runtimes on ONE data dir: each worker starts
    its own cron ticker, goal dispatcher, curator and settlement watcher, and
    the settlement watcher's dedup lock is in-process only, so two of them race
    the same ``settlement_scan`` checkpoint and can double-apply a payment.

    This lives HERE, not in ``cli/commands/serve.py``, because ``main.py`` —
    the systemd entry — launches through :func:`run_server` and never sees the
    CLI. A refusal only the CLI enforces is a refusal the production entry
    point skips.
    """
    effective = resolve_workers(workers)
    if effective <= 1:
        return None
    from core.env import bool_env

    if not bool_env("API_AUTONOMY_RUNTIME", True):
        return None
    return (
        f"Refusing to start {effective} workers while this process "
        "runs the autonomy loops.\n"
        "Each worker would start its own cron/goal/curator/settlement "
        "runtime on the same data dir, and two settlement watchers can "
        "double-apply a payment.\n"
        "Run the loops in exactly one process: start the extra workers with "
        "API_AUTONOMY_RUNTIME=false, or keep --workers 1."
    )


def run_server(host=None, port=None, workers=None, *, reload=None, log_level=None):
    """Launch the uvicorn server.

    The single reusable launch path shared by ``main.py`` (the systemd entry)
    and the ``polyrob serve`` subcommand. Any argument left as ``None`` falls
    back to its ``UVICORN_*`` / ``LOG_LEVEL`` environment default.
    """
    import sys

    import uvicorn

    # Load environment
    _load_env()

    # B13: the multi-worker/autonomy-runtime refusal runs on EVERY launch path,
    # not only `polyrob serve` — env is loaded above, so API_AUTONOMY_RUNTIME
    # is read from the same layering the app will see.
    refusal = multiworker_refusal(workers)
    if refusal:
        print(refusal, file=sys.stderr)
        raise SystemExit(1)

    # Get server configuration (explicit args win; else env defaults)
    if host is None:
        host = os.environ.get("UVICORN_HOST", "127.0.0.1")
    if port is None:
        port = int(os.environ.get("UVICORN_PORT", "9000"))
    workers = resolve_workers(workers)
    if reload is None:
        reload = os.environ.get("UVICORN_RELOAD", "false").lower() == "true"
    if log_level is None:
        log_level = os.environ.get("LOG_LEVEL", "info").lower()

    print(f"Starting uvicorn server on {host}:{port}")
    print(f"Workers: {workers}, Reload: {reload}, Log level: {log_level}")

    # Run uvicorn with factory function
    uvicorn.run(
        APP_TARGET,
        host=host,
        port=port,
        workers=workers if not reload else 1,  # Can't use multiple workers with reload
        reload=reload,
        log_level=log_level,
        # Proxy headers for nginx. Trust X-Forwarded-For ONLY from the local reverse
        # proxy by default: "*" lets any direct caller claim 127.0.0.1 and pass every
        # localhost-only check (webview/server_launcher.py pins the same default).
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("UVICORN_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        factory=True
    )
