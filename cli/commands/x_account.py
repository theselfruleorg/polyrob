"""polyrob x-account — manage the agent's X (x.com) browser account.

Distinct from ``polyrob x`` (the X DM surface). Subcommands:

- ``capture-session`` — owner logs in once in a visible browser; the resulting
  storage_state is stored ENCRYPTED so the agent can post headless afterwards.
  This is also how a locally-completed signup deploys to the VPS.
- ``status``          — show whether a session is stored + its handle.
- ``signup``          — autonomous account registration (added in Task 13).

The signup + posting rails escalate to the owner on any obstacle; nothing here
defeats a CAPTCHA.
"""
import asyncio
import os
import sys

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
def capture_session(timeout: int):
    """Open a visible browser, let the owner log in, store the session encrypted."""
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + "no DISPLAY — capture-session needs a visible browser. Run it "
                     "on your desktop, then deploy the session file to the server.")
        sys.exit(1)
    asyncio.run(_capture(timeout))


async def _capture(timeout: int):
    from playwright.async_api import async_playwright

    from tools.x_browser.driver import XPageDriver

    click.echo("opening a browser at x.com/login — log in as the agent's account…")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
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
