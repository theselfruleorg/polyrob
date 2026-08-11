"""`polyrob serve` — launch the API/webgate server (doc 01, T3).

A thin Click subcommand that delegates to ``api.server_boot.run_server`` — the
SAME uvicorn-launch callable the legacy ``python main.py`` systemd entry uses
(main.py is a shim over the same module). No launch logic is duplicated here;
this just maps CLI options onto that callable.

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

    # Non-interactive preflight FIRST — before any server import — so the
    # refusal message is reachable on every install: a server must not spin up
    # uvicorn only to crash in the lifespan when no usable provider key is
    # present. local_mode=True loads the same env layers every other CLI gate
    # reads (incl. ~/.polyrob/.env), so a key that works for `polyrob run`
    # also works here.
    from core.bootstrap import load_env
    from modules.llm.profiles import usable_providers_with_keys, no_key_message
    load_env(local_mode=True)
    if not usable_providers_with_keys(os.environ):
        click.echo(no_key_message(), err=True)
        sys.exit(1)

    # run_server lives in the installed `api` package (NOT repo-root main.py,
    # which ships in no wheel — importing it here broke every installed
    # `polyrob serve` with ModuleNotFoundError before the gate could print).
    try:
        from api.server_boot import run_server
    except ImportError as exc:
        click.echo(
            f"server dependencies unavailable ({exc}) — install them with "
            "`pip install 'polyrob[server]'`",
            err=True,
        )
        sys.exit(1)

    run_server(host=host, port=port, workers=workers)
