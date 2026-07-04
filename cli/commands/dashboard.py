"""polyrob dashboard — launch the local web dashboard (webgate).

The webgate is the *self-host owner's* web UI: chat, sessions, memory, autonomy,
identity, system. By default it runs **single-user, local-first** — bound to
loopback (127.0.0.1:5050), no auth, every session owned by the local owner
(Posture 0 / "local"). Pass ``--posture own_ops`` for a public status page +
owner-login-gated console, or ``--multitenant`` (legacy alias for
``--posture multitenant``) to engage the full JWT/SIWE + admin layer.

Mirrors `cli/commands/telegram.py`'s click-command shape; registered in
`cli/polyrob.py`.

NOTE: the dashboard is a viewer + chat UI; it does NOT run the autonomy loops
(cron/goals/curator). Goals/cron created via its pages execute when a worker with
the autonomy runtime is up (`polyrob serve` / `polyrob gateway` / the REPL under
ROB_LOCAL) — not from the dashboard alone.
"""
import os
import webbrowser

import click


@click.command(short_help="Launch the local web dashboard (webgate)")
@click.option("--multitenant", is_flag=True,
              help="Enable the multitenant layer (JWT/SIWE auth + admin pages, bind 0.0.0.0). "
                   "Alias for --posture multitenant.")
@click.option("--posture", type=click.Choice(["local", "own_ops", "multitenant"]), default=None,
              help="Deployment posture (default: local, or derived from --host if non-loopback)")
@click.option("--host", default=None, help="Bind address (default 127.0.0.1 single-user)")
@click.option("--port", type=int, default=None, help="Port to listen on (default 5050)")
@click.option("--no-browser", is_flag=True, help="Do not open a browser window")
def dashboard(multitenant, posture, host, port, no_browser):
    """Run the polyrob webgate (local web dashboard)."""
    # Precedence: --multitenant (legacy alias) > --posture > env already set > default local.
    # Must be set BEFORE importing webview.server (it reads the flag at import
    # time to mount-gate routes).
    if multitenant:
        os.environ["WEBGATE_MULTITENANT"] = "true"
    elif posture:
        os.environ["POLYROB_POSTURE"] = posture
    else:
        os.environ.setdefault("WEBGATE_MULTITENANT", "false")

    # Safe-by-default fix: an explicit --host is a *local* CLI variable that
    # otherwise only reaches uvicorn.run() directly — it never fed webgate's
    # posture derivation, so `polyrob dashboard --host 0.0.0.0` (no --posture)
    # would bind publicly while webgate.posture() stayed "local" (no auth).
    # Feed it into WEBGATE_HOST so webgate.posture()'s host-derivation
    # (loopback -> local, else -> own_ops) sees the same host uvicorn binds to.
    # No effect when --multitenant/--posture already set POLYROB_POSTURE /
    # WEBGATE_MULTITENANT above (those win outright in webgate.posture()).
    if host:
        os.environ["WEBGATE_HOST"] = host

    # Key check: the dashboard is a viewer + chat UI, so a missing key is a WARN (the
    # UI still opens for view-only pages) rather than a hard exit — but surface it now
    # instead of letting chat fail deep in the request handler.
    from core.bootstrap import load_env
    from modules.llm.profiles import usable_providers_with_keys
    load_env(local_mode=True)
    if not usable_providers_with_keys(os.environ):
        click.echo(click.style("[polyrob] WARN: ", fg="yellow")
                   + "no usable provider key — chat will fail until you set one "
                     "(`polyrob init` / `polyrob config set`). View-only pages still work.")

    from webview import webgate

    bind_host = host or webgate.bind_host()
    bind_port = port or webgate.bind_port()

    # A 0.0.0.0 bind is reachable locally via loopback — show a clickable URL.
    display_host = "127.0.0.1" if bind_host in ("0.0.0.0", "") else bind_host
    url = f"http://{display_host}:{bind_port}"

    click.echo(click.style("polyrob webgate", fg="green") + f"  →  {url}")
    current_posture = webgate.posture()
    if current_posture == "multitenant":
        click.echo(click.style(
            "multitenant mode: auth + admin pages enabled, bound on all interfaces.",
            fg="yellow"))
    elif current_posture == "own_ops":
        click.echo(click.style(
            "own_ops mode: minimal public status page at /, owner login required for the "
            "console. Set POLYROB_OWNER_USERNAME/POLYROB_OWNER_PASSWORD_HASH.",
            fg="yellow"))
    else:
        click.echo(click.style(
            "single-user mode: no auth, loopback only (the owner is you).", dim=True))

    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    import uvicorn

    # Import AFTER the flags are set so the route table is built for the chosen posture.
    from webview.server import app

    click.echo(click.style(f"binding {bind_host}:{bind_port} … (Ctrl-C to stop)", dim=True))
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
