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
    from modules.llm.profiles import usable_providers_with_credentials, no_key_message
    load_env(local_mode=True)
    if not usable_providers_with_credentials(os.environ):
        click.echo(no_key_message(), err=True)
        sys.exit(1)

    # 027 WP3: api.server_boot itself imports cleanly without the extra (uvicorn
    # is imported inside run_server), so a try/except around the import was dead
    # code and users got a raw ModuleNotFoundError. Probe the actual deps.
    from cli.commands._errors import require_extra_or_exit
    require_extra_or_exit("server")

    # B13: N workers = N autonomy runtimes on ONE data dir. The rule lives in
    # `api.server_boot` — the launch path `main.py` (the systemd entry) shares —
    # so a refusal the CLI enforces is not a refusal production skips. Checked
    # here too only so the operator sees it before uvicorn is imported.
    from api.server_boot import multiworker_refusal, run_server

    refusal = multiworker_refusal(workers)
    if refusal:
        click.echo(refusal, err=True)
        sys.exit(1)

    run_server(host=host, port=port, workers=workers)
