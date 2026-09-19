"""polyrob x-account — manage the agent's X (x.com) browser account.

Distinct from ``polyrob x`` (the X DM surface). Subcommands:

- ``capture-session`` — owner logs in once in a visible browser; the resulting
  storage_state is stored ENCRYPTED so the agent can post headless afterwards.
  ``--out <file>`` ALSO writes the plain Playwright storage_state JSON — the
  hand-off from a desktop capture to a headless server.
- ``import-session``  — the server half: read that JSON (or just the two X
  login cookies ``auth_token`` + ``ct0``) and store it under THIS box's key
  and identity. The encrypted store cannot simply be copied between machines
  (Fernet key + identity are per-box), which is why this verb exists.
- ``status``          — show whether a session is stored + its handle.
- ``signup``          — autonomous account registration (added in Task 13).

The signup + posting rails escalate to the owner on any obstacle; nothing here
defeats a CAPTCHA.
"""
import asyncio
import os
import sys
from typing import Optional

import click


def _store():
    from tools.x_browser.session_store import XSessionStore
    return XSessionStore()


def _user_id() -> str:
    from core.identity import resolve_identity
    return resolve_identity()


@click.group(name="x-account")
def x_account():
    """Manage the agent's own X (x.com) browser account."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@x_account.command("status")
def status():
    """Show the stored X session (presence + handle)."""
    store = _store()
    uid = _user_id()
    record = store.load(uid)
    if not record:
        click.echo(click.style("no X session captured", fg="yellow")
                   + " — run `polyrob x-account capture-session`.")
        return
    handle = record.get("handle") or "(unknown handle)"
    created = record.get("created_at") or "?"
    click.echo(click.style("X session stored", fg="green")
               + f": @{handle} (captured {created})")
    click.echo(click.style(
        "validity is checked live by the agent's x_login_check verb.", dim=True))


@x_account.command("capture-session")
@click.option("--timeout", default=300, type=int,
              help="Seconds to wait for the owner to finish logging in.")
@click.option("--out", "out", type=click.Path(dir_okay=False), default=None,
              help="Also write the plain storage_state JSON here (0600) for "
                   "`polyrob x-account import-session <file>` on the server.")
def capture_session(timeout: int, out):
    """Open a visible browser, let the owner log in, store the session encrypted."""
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + "no DISPLAY — capture-session needs a visible browser. Run it "
                     "on your desktop with --out x-session.json, copy that file to "
                     "the server and run `polyrob x-account import-session "
                     "x-session.json` there.")
        sys.exit(1)
    asyncio.run(_capture(timeout, out=out))


# The two cookies X's web client authenticates with. A storage_state without
# `auth_token` is a logged-out browser; importing it would store a session the
# agent then reports as "not logged in" on its first x_login_check.
_LOGIN_COOKIES = ("auth_token", "ct0")


def _write_plain_state(path: str, storage_state: dict) -> None:
    import json
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(storage_state, fh)


def _cookie_state(auth_token: str, ct0: str) -> dict:
    """A minimal Playwright storage_state carrying only the two login cookies,
    shaped exactly as `context.storage_state()` emits them for x.com."""
    return {
        "cookies": [
            {"name": "auth_token", "value": auth_token, "domain": ".x.com", "path": "/",
             "expires": -1, "httpOnly": True, "secure": True, "sameSite": "None"},
            {"name": "ct0", "value": ct0, "domain": ".x.com", "path": "/",
             "expires": -1, "httpOnly": False, "secure": True, "sameSite": "Lax"},
        ],
        "origins": [],
    }


def _validate_state(storage_state: dict) -> Optional[str]:
    """None when the state carries an X login; else the reason it does not."""
    if not isinstance(storage_state, dict) or not isinstance(storage_state.get("cookies"), list):
        return "not a Playwright storage_state (expected {\"cookies\": [...], \"origins\": [...]})"
    names = {c.get("name") for c in storage_state["cookies"] if isinstance(c, dict)}
    missing = [n for n in _LOGIN_COOKIES if n not in names]
    if missing:
        return ("no X login in this file — missing cookie(s): " + ", ".join(missing)
                + " (export it from a browser that is signed in to x.com)")
    return None


@x_account.command("import-session")
@click.argument("state_file", required=False, type=click.Path(exists=True, dir_okay=False))
@click.option("--auth-token", default=None,
              help="The x.com `auth_token` cookie value (alternative to a file).")
@click.option("--ct0", default=None, help="The x.com `ct0` cookie value (with --auth-token).")
@click.option("--handle", default=None, help="The account's @handle (without the @).")
def import_session(state_file, auth_token, ct0, handle):
    """Store an X login captured elsewhere, encrypted under THIS box's key.

    Source is either a Playwright storage_state JSON (from `capture-session --out`
    or `context.storage_state()`), or the two login cookies `auth_token` + `ct0`
    copied from a signed-in desktop browser (DevTools → Application → Cookies →
    x.com). Delete the plain file afterwards — it IS the login.
    """
    import json
    if state_file and (auth_token or ct0):
        raise click.UsageError("give a storage_state file OR --auth-token/--ct0, not both")
    if state_file:
        try:
            with open(state_file) as fh:
                storage_state = json.load(fh)
        except (OSError, ValueError) as e:
            raise click.ClickException(f"cannot read {state_file}: {e}")
    elif auth_token and ct0:
        storage_state = _cookie_state(auth_token.strip(), ct0.strip())
    else:
        raise click.UsageError("nothing to import: give a storage_state file, or both "
                               "--auth-token and --ct0")
    why = _validate_state(storage_state)
    if why:
        raise click.ClickException(why)
    handle = (handle or "").lstrip("@").strip() or None
    _store().save(_user_id(), storage_state=storage_state, handle=handle)
    click.echo(click.style("imported", fg="green")
               + f": stored encrypted X session for @{handle or 'unknown'} "
                 f"({len(storage_state['cookies'])} cookie(s)).")
    click.echo("verify with `polyrob x-account status`; the agent checks validity live "
               "via x_login_check. Enable posting with X_BROWSER_ENABLED=true.")
    if state_file:
        click.echo(click.style(f"now delete {state_file} — it is the login in plain text.",
                               fg="yellow"))


async def _capture(timeout: int, out: Optional[str] = None):
    from playwright.async_api import async_playwright

    from tools.x_browser.driver import XPageDriver

    click.echo("opening a browser at x.com/login — log in as the agent's account…")
    from tools.browser.launch_security import desktop_launch_kwargs
    try:
        launch_kwargs = desktop_launch_kwargs(headless=False)
    except RuntimeError as e:  # custody: never launch Chromium beside the signer
        raise click.ClickException(
            f"{e} Run capture-session on your desktop, not on the custody host.")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://x.com/login", wait_until="domcontentloaded")
        driver = XPageDriver(page)

        # Poll until the owner has completed login (home chrome present).
        waited = 0.0
        while waited < timeout:
            if await driver.is_logged_in():
                break
            await asyncio.sleep(2.0)
            waited += 2.0
        else:
            click.echo(click.style("timed out waiting for login — nothing stored.",
                                   fg="red"))
            await browser.close()
            sys.exit(1)

        storage_state = await context.storage_state()
        handle = await _read_handle(page)
        await browser.close()

    _store().save(_user_id(), storage_state=storage_state, handle=handle)
    click.echo(click.style("captured", fg="green")
               + f": stored encrypted X session for @{handle or 'unknown'}.")
    if out:
        _write_plain_state(out, storage_state)
        click.echo(click.style("wrote", fg="green") + f" plain storage_state to {out} "
                   "(0600) — copy it to the server, run `polyrob x-account import-session "
                   f"{os.path.basename(out)} --handle {handle or '<handle>'}` there, "
                   "then delete both copies.")
    click.echo("enable agent inbox access with X_BROWSER_ENABLED=true, then load "
               "the x_browser tool (x_read_dms / x_dm).")


@x_account.command("signup")
@click.option("--resume", is_flag=True, help="Resume a previously paused signup.")
@click.option("--timeout", default=900, type=int,
              help="Seconds to allow per owner checkpoint.")
def signup(resume: bool, timeout: int):
    """Register the agent's own X account (autonomous; pings you on any obstacle).

    Runs headed when a display exists so you can solve a CAPTCHA in-window;
    headless, it pauses on any visual challenge and tells you how to resume.
    """
    headless = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
    if headless:
        click.echo(click.style(
            "note: no display — signup will PAUSE on any visual challenge and "
            "ask you to resume locally.", fg="yellow"))
    asyncio.run(_signup(resume, timeout, headless))


async def _signup(resume: bool, timeout: int, headless: bool):
    from playwright.async_api import async_playwright

    from tools.x_browser.driver import XPageDriver
    from tools.x_browser.escalation import EscalationOutcome, escalate_and_wait
    from tools.x_browser.session_store import XSessionStore
    from tools.x_browser.signup import MailPoller, SignupFlow, SignupPaused

    uid = _user_id()
    store = _store()
    if store.exists(uid) and (store.load(uid) or {}).get("storage_state") is not None:
        click.echo(click.style("an X account already exists for this agent.",
                               fg="yellow"))
        sys.exit(1)

    key = os.environ.get("AGENTMAIL_API_KEY", "")
    if not key:
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + "signup needs the agent's inbox for the verification code — "
                     "set AGENTMAIL_API_KEY.")
        sys.exit(1)

    from core.instance import (
        resolve_instance_id,
        resolve_owner_principal,
    )
    from tools.email_providers.agentmail import AgentMailClient

    from tools.browser.launch_security import desktop_launch_kwargs
    try:
        launch_kwargs = desktop_launch_kwargs(headless=headless)
    except RuntimeError as e:  # custody: never launch Chromium beside the signer
        raise click.ClickException(
            f"{e} Run `polyrob x-account signup` on your desktop (the session store "
            "is what the server reads), not on the custody host.")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context()
        page = await context.new_page()
        driver = XPageDriver(page)

        class _Identity:
            name = resolve_instance_id().capitalize()
            dob = "2000-01-01"
            handle = resolve_instance_id()
            disclosure = (f"Automated account (bot) operated by "
                          f"{resolve_owner_principal() or 'its owner'}.")

        mail = MailPoller(AgentMailClient(key))
        flow = SignupFlow(driver=driver, mail=mail, store=store,
                          progress=XSessionStore(provider="x_signup"),
                          identity=_Identity(), user_id=uid)
        do_resume = resume
        for _ in range(4):
            try:
                result = await flow.run(resume=do_resume)
            except SignupPaused as paused:
                click.echo(click.style(f"paused: {paused.prompt}", fg="yellow"))
                outcome = await escalate_and_wait(
                    None, uid, "", paused, timeout_sec=timeout,
                    driver=driver if not headless else None)
                if outcome is EscalationOutcome.CLEARED:
                    do_resume = True
                    continue
                await browser.close()
                click.echo(click.style(
                    "signup paused — resume with `polyrob x-account signup --resume` "
                    "once handled." if outcome is EscalationOutcome.PAUSED
                    else "signup stopped.", fg="yellow"))
                sys.exit(1)
            await browser.close()
            click.echo(click.style("done", fg="green")
                       + f": @{result.handle} ({result.address}).")
            return
        await browser.close()
        click.echo(click.style("still paused after several checkpoints.", fg="yellow"))


async def _read_handle(page) -> str:
    """Best-effort @handle read from the account switcher; '' on failure."""
    try:
        el = await page.query_selector('[data-testid="SideNav_AccountSwitcher_Button"]')
        if el:
            text = await el.inner_text()
            for token in text.split():
                if token.startswith("@"):
                    return token.lstrip("@")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# OAuth 2.0 user token (X Chat DM read) — store, import, PKCE login, refresh
# ---------------------------------------------------------------------------

def _oauth_status_lines(st: dict) -> list:
    lines = []
    if st.get("stored"):
        left = st.get("expires_in_sec", 0)
        state = "EXPIRED" if st.get("expired") else f"valid for {left // 60} min"
        lines.append(click.style("OAuth2 token stored", fg="green") + f": {state}"
                     f" · refresh token {'yes' if st.get('has_refresh_token') else 'NO'}"
                     f" · scope [{st.get('scope') or '?'}] · source {st.get('source') or '?'}")
    else:
        lines.append(click.style("no OAuth2 token stored", fg="yellow")
                     + (" (static TWITTER_OAUTH2_ACCESS_TOKEN env is set — it expires 2h "
                        "after mint and nothing refreshes it)" if st.get("static_env") else ""))
    lines.append(f"client id: {'set' if st.get('client_id_set') else 'MISSING (TWITTER_OAUTH2_CLIENT_ID)'}"
                 f" · client secret: {'set' if st.get('client_secret_set') else 'not set (public app)'}")
    if st.get("stored") and not st.get("has_refresh_token"):
        lines.append(click.style("⚠ no refresh token — the store cannot renew this token; "
                                 "re-run oauth-login with offline.access", fg="yellow"))
    return lines


@x_account.command("oauth-status")
def oauth_status():
    """Show the OAuth 2.0 user-token state (no secret values)."""
    from tools.x_oauth2 import status
    for line in _oauth_status_lines(status()):
        click.echo(line)


@x_account.command("oauth-import")
@click.option("--expires-in", type=int, default=None,
              help="Seconds of life left on the access token (default: assume a fresh 7200).")
def oauth_import(expires_in):
    """Store an access + refresh token pair minted elsewhere (hidden prompts).

    Use this when you ran the PKCE flow by hand. The pair is stored encrypted
    and refreshed automatically from then on; the env values are not needed.
    """
    from tools.x_oauth2 import import_pair, status
    access = click.prompt("access token", hide_input=True).strip()
    refresh = click.prompt("refresh token (blank = none)", hide_input=True,
                           default="", show_default=False).strip()
    import_pair(access, refresh, expires_in=expires_in)
    click.echo(click.style("stored", fg="green") + " — encrypted in the X token store.")
    for line in _oauth_status_lines(status()):
        click.echo(line)
    if not (os.environ.get("TWITTER_OAUTH2_CLIENT_ID") or "").strip():
        click.echo(click.style("⚠ TWITTER_OAUTH2_CLIENT_ID is not set: the token can be USED "
                               "but not REFRESHED. Set it (and the secret for a confidential "
                               "app) in the instance env.", fg="yellow"))


@x_account.command("oauth-refresh")
def oauth_refresh():
    """Refresh the stored token now (proves the client id/secret + refresh token work)."""
    from tools.x_oauth2 import refresh, status
    try:
        refresh()
    except Exception as e:
        raise click.ClickException(f"refresh failed: {e}")
    click.echo(click.style("refreshed", fg="green"))
    for line in _oauth_status_lines(status()):
        click.echo(line)


@x_account.command("oauth-login")
@click.option("--port", default=8765, type=int, help="Local callback port (must match the app's redirect URI).")
@click.option("--timeout", default=600, type=int, help="Seconds to wait for the browser redirect.")
@click.option("--no-browser", is_flag=True, default=False, help="Print the URL instead of opening a browser.")
def oauth_login(port: int, timeout: int, no_browser: bool):
    """Mint the OAuth 2.0 user token with a PKCE flow (login AS the agent's account).

    Needs TWITTER_OAUTH2_CLIENT_ID (+ _CLIENT_SECRET for a confidential app) and
    the app's redirect URI set to http://127.0.0.1:<port>/callback. Stores the
    pair encrypted; refreshes are automatic afterwards.
    """
    import secrets as _secrets
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    from tools.x_oauth2 import authorize_url, exchange_code, pkce_pair, status

    redirect_uri = f"http://127.0.0.1:{port}/callback"
    verifier, challenge = pkce_pair()
    state = _secrets.token_urlsafe(16)
    try:
        url = authorize_url(redirect_uri=redirect_uri, state=state, code_challenge=challenge)
    except Exception as e:
        raise click.ClickException(str(e))

    result: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            result["code"] = (q.get("code") or [""])[0]
            result["state"] = (q.get("state") or [""])[0]
            result["error"] = (q.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"polyrob: you can close this tab.")

        def log_message(self, *a):  # silence
            return

    server = HTTPServer(("127.0.0.1", port), _Handler)
    server.timeout = 1.0
    click.echo("open this URL logged in AS the agent's X account:\n  " + url)
    if not no_browser:
        webbrowser.open(url)
    waited = 0.0
    while "code" not in result and waited < timeout:
        server.handle_request()
        waited += 1.0
    server.server_close()
    if "code" not in result:
        raise click.ClickException("timed out waiting for the redirect — nothing stored.")
    if result.get("error"):
        raise click.ClickException(f"X refused: {result['error']}")
    if result.get("state") != state:
        raise click.ClickException("state mismatch — refusing the code (CSRF guard).")
    try:
        exchange_code(result["code"], redirect_uri=redirect_uri, code_verifier=verifier)
    except Exception as e:
        raise click.ClickException(f"token exchange failed: {e}")
    click.echo(click.style("stored", fg="green") + " — encrypted OAuth2 pair for the agent's X account.")
    for line in _oauth_status_lines(status()):
        click.echo(line)
