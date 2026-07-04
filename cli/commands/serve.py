"""`polyrob serve` — launch the API/webgate server (doc 01, T3).

A thin Click subcommand that delegates to ``main.run_server`` — the SAME
uvicorn-launch callable the legacy ``python main.py`` systemd entry uses. No
launch logic is duplicated here; this just maps CLI options onto that callable.

``main.py`` stays the systemd entry point until doc 06 flips the unit file to the
``polyrob serve`` entry.
"""

import click


@click.command()
@click.option("--host", default=None, help="Bind address (default: UVICORN_HOST env, else 127.0.0.1).")
@click.option("--port", default=None, type=int, help="Port to listen on (default: UVICORN_PORT env, else 9000).")
@click.option(
    "--workers",
    default=None,
    type=int,
    help="Number of uvicorn workers (default: env UVICORN_WORKERS, else 1).",
)
def serve(host, port, workers):
    """Launch the POLYROB API server (uvicorn)."""
    # Lazy import keeps this command dependency-light at module import time.
    import os
    import sys

    from main import run_server

    # Non-interactive preflight: a server must not spin up uvicorn only to crash in the
    # lifespan when no usable provider key is present. Print the canonical message and
    # exit cleanly instead. (The lifespan LLMError is the backstop for `python main.py`.)
    from core.bootstrap import load_env
    from modules.llm.profiles import usable_providers_with_keys, no_key_message
    load_env(local_mode=False)
    if not usable_providers_with_keys(os.environ):
        click.echo(no_key_message(), err=True)
        sys.exit(1)

    run_server(host=host, port=port, workers=workers)
