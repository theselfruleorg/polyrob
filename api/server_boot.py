"""Uvicorn launch path shared by ``polyrob serve`` and ``python main.py``.

This lives in the installed ``api`` package because repo-root ``main.py`` ships
in no wheel and console scripts never have the CWD on ``sys.path`` — importing
it from ``cli/commands/serve.py`` crashed every installed ``polyrob serve``
before its no-key preflight could even print (UX assessment 2026-08-07, B1).
``main.py`` stays the systemd entry point as a thin shim over this module.
"""

import os

# The ASGI app target uvicorn launches (factory function).
APP_TARGET = "api.app:get_app"


def _load_env():
    """Load the active env file (config/.env.<ENV> or .env)."""
    from dotenv import load_dotenv

    env = os.environ.get('ENV', 'development')
    env_file = f'config/.env.{env}'
    if os.path.exists(env_file):
        load_dotenv(env_file)
        print(f"Loaded environment from {env_file}")
    elif os.path.exists('.env'):
        load_dotenv('.env')
        print("Loaded environment from .env")


def run_server(host=None, port=None, workers=None, *, reload=None, log_level=None):
    """Launch the uvicorn server.

    The single reusable launch path shared by ``main.py`` (the systemd entry)
    and the ``polyrob serve`` subcommand. Any argument left as ``None`` falls
    back to its ``UVICORN_*`` / ``LOG_LEVEL`` environment default.
    """
    import uvicorn

    # Load environment
    _load_env()

    # Get server configuration (explicit args win; else env defaults)
    if host is None:
        host = os.environ.get("UVICORN_HOST", "127.0.0.1")
    if port is None:
        port = int(os.environ.get("UVICORN_PORT", "9000"))
    if workers is None:
        workers = int(os.environ.get("UVICORN_WORKERS", "1"))
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
        # Proxy headers for nginx
        proxy_headers=True,
        forwarded_allow_ips="*",
        factory=True
    )
