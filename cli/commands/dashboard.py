"""polyrob dashboard — launch the POLYROB Console (webgate).

The webgate is the *self-host owner's* web UI: chat, sessions, memory, autonomy,
identity, system. By default it runs **single-user, local-first** — bound to
loopback (127.0.0.1:5050), with owner login
(Posture 0 / "local"). Pass ``--posture own_ops`` for a public status page +
owner-login-gated console, or ``--multitenant`` (legacy alias for
``--posture multitenant``) to engage the full JWT/SIWE + admin layer.

Mirrors `cli/commands/telegram.py`'s click-command shape; registered in
`cli/polyrob.py`.

NOTE: the dashboard is a viewer + chat UI; it does NOT run the autonomy loops
(cron/goals/curator). Goals/cron created via its pages execute when a worker with
the autonomy runtime is up (`polyrob serve` / `polyrob gateway` / the REPL under
POLYROB_LOCAL) — not from the dashboard alone.
"""
import os
import webbrowser

import click


def _anchor_session_root() -> None:
    """Anchor the console's session tree on the data home.

    With neither ``DATA_ROOT`` nor ``POLYROB_DATA_DIR`` set, the shared resolver
    falls back to the legacy ``./data/task`` and the PathManager creates it in the
    caller's cwd — a stray ``data/task`` dir, and a different tree from the one
    ``polyrob`` in the same folder writes (``<data home>/sessions``). The dashboard
    runs in the caller's folder, so the CLI's local data home is the right root.
    An operator-set ``DATA_ROOT`` or ``POLYROB_DATA_DIR`` always wins.
    """
    if (os.environ.get("DATA_ROOT") or "").strip():
        return
    if (os.environ.get("POLYROB_DATA_DIR") or "").strip():
        return
    from core.runtime_paths import resolve_data_home
    os.environ["DATA_ROOT"] = str(resolve_data_home() / "sessions")


#: The username a first-run local console signs in with when none is set.
DEFAULT_OWNER_USERNAME = "owner"


def _persist_flag(key: str, value: str) -> bool:
    """Write *key* to the global env file (``~/.polyrob/.env``) via the one
    config writer. True on success; the value is set in this process either way."""
    os.environ[key] = value
    try:
        from core import config_service
        res = config_service.set_value(key, value, scope="global", surface="local")
        return bool(res.ok)
    except Exception:
        return False


def ensure_jwt_secret() -> None:
    """A local console mints its own login-cookie key on first run.

    ``assert_login_configured`` refuses a console without a 32+ character
    ``JWT_SECRET_KEY``; on a workstation nobody should have to invent one. The
    key is random and kept in the global env file, so a sign-in survives a
    restart. An operator-set key always wins."""
    if len(str(os.environ.get("JWT_SECRET_KEY") or "").strip()) >= 32:
        return
    import secrets
    if not _persist_flag("JWT_SECRET_KEY", secrets.token_urlsafe(48)):
        click.echo(click.style("[polyrob] WARN: ", fg="yellow")
                   + "could not save the console login key — sign-ins end when "
                     "this console stops.")


def _hash_password(password: str) -> str:
    from argon2 import PasswordHasher
    return PasswordHasher().hash(password)


def ensure_local_owner_login() -> None:
    """A local console with no owner password gets a ONE-TIME password.

    Loopback is NOT the owner (any local process reaches it), so the console
    still requires a sign-in. With no ``POLYROB_OWNER_PASSWORD_HASH`` the
    dashboard makes a random password for THIS run only, prints it here — the
    terminal the owner just typed into — and names the command that sets a
    lasting one. Nothing is written to disk."""
    if str(os.environ.get("POLYROB_OWNER_PASSWORD_HASH") or "").strip():
        # A lasting password is set: never replace it with a one-time one. A
        # missing username falls back to the default the hash was saved with.
        if not str(os.environ.get("POLYROB_OWNER_USERNAME") or "").strip():
            os.environ["POLYROB_OWNER_USERNAME"] = DEFAULT_OWNER_USERNAME
        return
    import secrets
    username = (str(os.environ.get("POLYROB_OWNER_USERNAME") or "").strip()
                or DEFAULT_OWNER_USERNAME)
    password = secrets.token_urlsafe(12)
    os.environ["POLYROB_OWNER_USERNAME"] = username
    os.environ["POLYROB_OWNER_PASSWORD_HASH"] = _hash_password(password)
    click.echo(click.style("Sign in with", fg="green")
               + f"  username: {username}   password: {password}")
    click.echo(click.style(
        "This password is for this run only. Set a lasting one with "
        "`polyrob dashboard --set-password`.", dim=True))


def set_password_interactive() -> None:
    """``polyrob dashboard --set-password``: save a lasting owner login."""
    username = click.prompt(
        "Owner username",
        default=(os.environ.get("POLYROB_OWNER_USERNAME") or DEFAULT_OWNER_USERNAME))
    password = click.prompt("New password", hide_input=True,
                            confirmation_prompt=True)
    if len(password) < 8:
        raise click.ClickException("the password must have at least 8 characters")
    ok = (_persist_flag("POLYROB_OWNER_USERNAME", str(username).strip())
          and _persist_flag("POLYROB_OWNER_PASSWORD_HASH", _hash_password(password)))
    ensure_jwt_secret()
    if not ok:
        raise click.ClickException(
            "could not write ~/.polyrob/.env — set POLYROB_OWNER_USERNAME and "
            "POLYROB_OWNER_PASSWORD_HASH yourself")
    click.echo(click.style("Saved.", fg="green")
               + " The console signs in with this password from the next start.")


@click.command(short_help="Launch the POLYROB Console (webgate)")
@click.option("--multitenant", is_flag=True,
              help="Enable the multitenant layer (JWT/SIWE auth + admin pages, bind 0.0.0.0). "
                   "Alias for --posture multitenant.")
@click.option("--posture", type=click.Choice(["local", "own_ops", "multitenant"]), default=None,
              help="Deployment posture (default: local, or derived from --host if non-loopback)")
@click.option("--host", default=None, help="Bind address (default 127.0.0.1 single-user)")
@click.option("--port", type=int, default=None, help="Port to listen on (default 5050)")
@click.option("--no-browser", is_flag=True, help="Do not open a browser window")
@click.option("--set-password", "set_password", is_flag=True,
              help="Save a lasting owner username + password for the console, then exit")
def dashboard(multitenant, posture, host, port, no_browser, set_password):
    """Run the POLYROB Console."""
    # 027 WP3: fail on a missing [server] extra BEFORE printing the URL and
    # opening a browser tab (it used to crash with a raw traceback after both).
    from cli.commands._errors import require_extra_or_exit
    require_extra_or_exit("server")
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
    from modules.llm.profiles import usable_providers_with_credentials
    load_env(local_mode=True)
    if not usable_providers_with_credentials(os.environ):
        click.echo(click.style("[polyrob] WARN: ", fg="yellow")
                   + "no usable provider key — chat will fail until you set one "
                     "(`polyrob init` / `polyrob config set`). View-only pages still work.")

    _anchor_session_root()

    if set_password:
        set_password_interactive()
        return

    from webview import webgate

    bind_host = host or webgate.bind_host()
    bind_port = port or webgate.bind_port()

    # Security review 2026-09-23 (Low): `--posture local --host 0.0.0.0` bound
    # the no-login console to every interface. Refuse unless the operator set
    # the documented override (they front it with their own auth layer).
    from webview.posture_guard import ALLOW_FLAG, _allow_override, non_loopback_bind
    if webgate.posture() == "local" and non_loopback_bind(bind_host) \
            and not _allow_override(os.environ):
        raise click.ClickException(
            f"refusing to bind {bind_host}: the 'local' posture is loopback-only. "
            "Use --posture own_ops, bind "
            f"127.0.0.1, or set {ALLOW_FLAG}=1 if your own auth layer fronts it.")

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
            "single-user mode: loopback only; owner login required.", dim=True))
        # A workstation console works on first run: its own cookie key, and a
        # one-time password printed here when no lasting one is set.
        ensure_jwt_secret()
        ensure_local_owner_login()

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
