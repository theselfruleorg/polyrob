"""`polyrob auth` — connect / list / remove / inspect LLM credentials (024 L2).

Owner-only, by construction: this is a CLI command, and no agent action reaches
``core.llm_auth`` (a permanent non-goal pinned by an AST test). Nothing here
ever prints a token — the only printable form is ``Credential.redacted()``.

`auth add <provider>` is the ONE connect verb, routed by the provider row:

- a row with an ``oauth`` block runs its declared OAuth flow (device code or
  PKCE; gated on ``LLM_OAUTH_ENABLED`` — some rows authenticate with a vendor
  CLI's own client id, so the ToS position is confirmed before the flow runs);
- a row with an ``env_key`` and no OAuth — every API-key provider, the
  key-based subscription plans (z.ai GLM Coding Plan, Cerebras Code, Ollama
  Cloud) included — gets the key connect flow (W2): signup URL + terms note,
  hidden key prompt, write to ``~/.polyrob/.env``, doctor echo, optional live
  validation. The z.ai pair additionally routes by PLAN choice, because
  ``ZAI_API_KEY`` (coding seat) and ``GLM_API_KEY`` (pay-as-you-go) hit
  different endpoints and a key works only on its own.

Custom rows come from YOUR ``providers.yaml``; an OAuth block looks like:

    providers:
      myplan:
        base_url: https://api.example.com/v1
        transport: chat_completions
        models: [some-model]
        oauth:
          auth_url: https://example.com/oauth/device      # device grant: the
                                                          # DEVICE endpoint
          token_url: https://example.com/oauth/token
          client_id: your-client-id
          scopes: [inference]
          grant: device_code            # or authorization_code (local only)
          refresh_skew_sec: 120
"""
from __future__ import annotations

import os

import click


def _fmt_expiry(epoch) -> str:
    """Relative expiry — an owner needs "does this need reconnecting soon",
    not a wall-clock stamp in whatever timezone the box is in."""
    import time
    if epoch is None:
        return "no expiry"
    delta = float(epoch) - time.time()
    if delta <= 0:
        return "EXPIRED"
    if delta < 3600:
        return f"expires in {int(delta // 60)}m"
    if delta < 86400:
        return f"expires in {int(delta // 3600)}h"
    return f"expires in {int(delta // 86400)}d"


@click.group("auth")
def auth():
    """Connect and inspect LLM provider credentials."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@auth.command("status")
@click.option("--json", "as_json", is_flag=True,
              help="Emit every provider's credential state as JSON.")
def status_cmd(as_json: bool):
    """Show every provider's credential: source, health, expiry.

    Reads the same oracle every gate reads, so what this prints IS what
    `polyrob run` will use — not a parallel view that can disagree with it.
    """
    from modules.llm.profiles import credential_status

    rows = credential_status()
    if as_json:
        import json as _json
        payload = {
            "providers": {
                name: {
                    "present": bool(st.present),
                    "usable": bool(st.usable),
                    "source": st.source,
                    "health": st.health,
                    "reason": st.reason,
                }
                for name, st in rows.items()
            }
        }
        click.echo(_json.dumps(payload, indent=2))
        return
    width = max((len(n) for n in rows), default=8)
    any_present = False
    for name, st in rows.items():
        if not st.present:
            continue
        any_present = True
        bits = [st.source]
        if st.health != "ok":
            bits.append(st.health)
        if st.expires_at:
            bits.append(_fmt_expiry(st.expires_at))
        if not st.usable:
            bits.append(f"UNUSABLE: {st.reason}")
        click.echo(f"  {name.ljust(width)}  {', '.join(bits)}")
    if not any_present:
        # 027 WP4: one remedy grammar everywhere — auth add first, init guided.
        click.echo("no provider credentials configured — connect one: "
                   "`polyrob auth add <provider>` (or `polyrob init` for guided setup)")

    absent = [n for n, s in rows.items() if not s.present]
    if absent:
        # Cap the wall like doctor does: 6 named + "(+N more)".
        shown, extra = absent[:6], len(absent) - 6
        tail = f" (+{extra} more — `polyrob model list`)" if extra > 0 else ""
        click.echo(f"\nnot configured: {', '.join(shown)}{tail}")


@auth.command("list")
def list_cmd():
    """List credentials held in the auth store (connected accounts only).

    Distinct from `status`: this is what the STORE holds, so an env-key-only
    box legitimately lists nothing here while `status` shows it as ready.
    """
    from core.llm_auth.store import auth_store_path, get_auth_store

    try:
        entries = get_auth_store().list_providers()
    except Exception as exc:
        raise click.ClickException(f"could not read the auth store: {exc}")
    if not entries:
        click.echo(f"auth store is empty ({auth_store_path()})")
        click.echo("connected accounts appear here; env-var keys do not — see `polyrob auth status`")
        return
    for name, entry in sorted(entries.items()):
        health = (entry.get("health") or {}).get("state") or "ok"
        click.echo(
            f"  {name}: {entry.get('token_type', 'Bearer')}, {health}, "
            f"{_fmt_expiry(entry.get('expires_at'))}"
            + (", refreshable" if entry.get("refresh_token") else ", no refresh token")
        )


def _open_browser(url: str):
    """Best-effort browser open. Never fatal — the URL is always printed too,
    which is the only thing that works over SSH anyway."""
    import webbrowser
    webbrowser.open(url)


def _spec_for(provider):
    from modules.llm.provider_spec import get_spec
    return get_spec(provider)


def _oauth_for(provider):
    """The provider's declared OAuth block.

    Resolved HERE, not in core: the provider registry lives in ``modules.llm``
    and core must not import upward (layering ratchet). The CLI is the tier that
    owns provider discovery, so it looks the row up and hands the block down.
    """
    from modules.llm.provider_spec import get_spec
    spec = get_spec(provider)
    if spec is None:
        raise click.ClickException(
            f"unknown provider '{provider}' — declare it in ~/.polyrob/providers.yaml "
            "(see `polyrob doctor --full` for the providers-file load report)"
        )
    return getattr(spec, "oauth", None)


# --- key-based connect (W2, HANDOFF-env-system-and-key-subscriptions) ---------
# A subscription plan that issues an API KEY (z.ai Coding Plan, Cerebras Code,
# Ollama Cloud) had working rows and clients but NO connect flow — `auth add`
# was OAuth-only, so the only way in was already knowing the magic var name.


def _stdin_isatty() -> bool:
    """Seam for tests; the real check drives prompt-vs-pipe behavior."""
    import sys
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


#: The z.ai pair: two products, two key vars, two endpoints. A key works ONLY
#: on its own endpoint, so the PLAN choice is the routing fact (spec comments
#: around the `zai`/`zai-coding` rows).
_ZAI_PLAN_ROWS = ("zai-coding", "zai")


def _resolve_zai_plan(spec):
    """Ask WHICH z.ai plan the key belongs to and return that row's spec.

    Interactive only — a piped run keeps the row the user named (deterministic
    for scripts; the prompt would otherwise eat the piped key line).
    """
    if getattr(spec, "name", None) not in _ZAI_PLAN_ROWS or not _stdin_isatty():
        return spec
    from modules.llm.provider_spec import get_spec
    click.echo("\nz.ai has two products with SEPARATE keys "
               "(a key works only on its own endpoint):")
    click.echo("  1) GLM Coding Plan (flat seat)  -> ZAI_API_KEY  (provider zai-coding)")
    click.echo("  2) pay-as-you-go API            -> GLM_API_KEY  (provider zai)")
    default = "1" if spec.name == "zai-coding" else "2"
    choice = click.prompt("Which plan is your key for?",
                          type=click.Choice(["1", "2"]), default=default)
    return get_spec("zai-coding" if choice == "1" else "zai") or spec


def _probe_key(spec, key):
    """Live-validate *key* against the row's endpoint (W2.2, cheap + fail-open).

    Returns ``(ok, detail)``: True = verified, False = the endpoint REJECTED
    the key (401/403 — the wrong-plan/wrong-var case caught immediately),
    None = inconclusive (no fixed endpoint, odd status, network error).
    Never raises; never logs the key.
    """
    import httpx
    try:
        base = (spec.resolved_base_url(api_key=key) or "").rstrip("/")
        if not base:
            return None, "no fixed endpoint to probe (SDK default base URL)"
        transport = getattr(spec.transport, "value", str(spec.transport))
        if transport == "anthropic_messages":
            model = spec.default_model or (spec.models[0] if spec.models else None)
            if not model:
                return None, "no model id to probe with"
            headers = {"anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            if getattr(spec, "bearer_auth", False):
                headers["Authorization"] = f"Bearer {key}"
            else:
                headers["x-api-key"] = key
            resp = httpx.post(f"{base}/v1/messages", headers=headers, json={
                "model": model, "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }, timeout=15)
        else:
            resp = httpx.get(f"{base}/models",
                             headers={"Authorization": f"Bearer {key}"}, timeout=15)
        if resp.status_code in (401, 403):
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        if resp.status_code == 200:
            return True, f"HTTP 200 from {base}"
        return None, f"HTTP {resp.status_code} from {base} (auth not clearly rejected)"
    except Exception as e:
        return None, str(e)


def _add_key_based(spec, validate_opt):
    """Connect a key-based provider: signup + ToS, hidden prompt, global write,
    doctor-style echo, optional live probe. Reuses `config set`'s prompt and
    writer paths — no second env writer."""
    import os

    from cli.commands.config import _prompt_for_value, _write_env_flag

    spec = _resolve_zai_plan(spec)
    env_key = spec.env_key
    click.echo(f"\nConnect {spec.display_name} ({spec.name}) with an API key.")
    if getattr(spec, "signup_url", None):
        click.echo(f"  Get a key: {spec.signup_url}")
    note = getattr(spec, "tos_note", None)
    if note:
        # §7.4: the terms-of-service position is stated BEFORE anything is
        # stored. Interactive runs confirm; a piped run has already committed.
        click.echo(click.style("\nBefore you connect:", bold=True))
        click.echo(f"  {note}")
        if _stdin_isatty() and not click.confirm("Continue?", default=True):
            click.echo("cancelled — nothing was stored")
            return

    value = _prompt_for_value(env_key)
    path = _write_env_flag(env_key, value, is_global=True)
    click.echo(f"saved {env_key} to {path}")

    # Echo the doctor line so the user sees exactly what `polyrob doctor` will
    # say — including "present but unusable" for a malformed paste.
    try:
        from modules.llm.profiles import credential_status
        env = dict(os.environ)
        env[env_key] = value
        st = credential_status(env).get(spec.name)
        if st is not None:
            readiness = ("present" if st.usable
                         else f"present but unusable — {st.reason}" if st.present
                         else "missing")
            tag = (" [subscription — not metered per token]"
                   if getattr(spec, "subscription", False) else "")
            click.echo(f"doctor: {spec.name}: {readiness}{tag}")
    except Exception:
        pass

    do_validate = validate_opt
    if do_validate is None:
        do_validate = _stdin_isatty() and click.confirm(
            "Validate the key with a live call now?", default=True)
    if do_validate:
        ok, detail = _probe_key(spec, value)
        if ok:
            click.echo(click.style(f"key verified ({detail})", fg="green"))
        elif ok is False:
            # The wrong-plan / wrong-var case, caught NOW instead of at the
            # first real run.
            click.echo(click.style(
                f"the endpoint REJECTED this key: {detail}", fg="red"))
            # 027 WP4: a kept-but-rejected key satisfies looks_like_real_key and
            # silences the no-key warning forever — offer to remove it here.
            removed = False
            if _stdin_isatty() and not click.confirm(
                    "Keep the rejected key anyway?", default=False):
                from core.env_file import remove_env_var
                if remove_env_var(path, env_key):
                    os.environ.pop(env_key, None)
                    click.echo(f"removed {env_key} from {path}")
                    removed = True
            if not removed:
                click.echo(f"  wrong plan or variable? clear it: "
                           f"polyrob config unset {env_key} --global")
        else:
            click.echo(f"could not validate ({detail}) — the key was saved; "
                       "the first real run will tell")


@auth.command("add")
@click.argument("provider")
@click.option("--validate/--no-validate", "validate_opt", default=None,
              help="Probe the endpoint with the new key after saving "
                   "(key-based rows; default: ask on a terminal, skip when piped)")
def add_cmd(provider, validate_opt):
    """Connect PROVIDER — its OAuth flow, or a key-based connect.

    A row that declares an OAuth block runs the OAuth flow (gated on
    LLM_OAUTH_ENABLED). A row with an env key and no OAuth — every API-key
    provider, subscription key plans included — gets the key connect flow:
    signup URL + terms note, hidden key prompt, write to ~/.polyrob/.env,
    then the doctor line and an optional live validation.
    """
    from core.llm_auth.flows import FlowError, connect, oauth_enabled

    spec = _spec_for(provider)
    if spec is None:
        raise click.ClickException(
            f"unknown provider '{provider}' — declare it in ~/.polyrob/providers.yaml "
            "(see `polyrob doctor --full` for the providers-file load report)"
        )
    if getattr(spec, "oauth", None) is None:
        if getattr(spec, "env_key", None):
            return _add_key_based(spec, validate_opt)
        raise click.ClickException(
            f"{spec.name} needs no credential (keyless endpoint) — nothing to connect"
        )

    if not oauth_enabled():
        raise click.ClickException(
            "OAuth connect is disabled. Set LLM_OAUTH_ENABLED=true to enable it "
            "(`polyrob config set LLM_OAUTH_ENABLED true`). It is off by default "
            "on every deployment including local — connecting a subscription "
            "seat is a deliberate act with a terms-of-service dimension."
        )

    def on_device_prompt(device):
        uri = device.get("verification_uri_complete") or device.get("verification_uri")
        click.echo(f"\n  Open: {uri}")
        click.echo(f"  Code: {device.get('user_code')}\n")
        click.echo("waiting for authorization… (Ctrl-C to cancel)")

    def on_url_prompt(url):
        click.echo(f"\n  Open: {url}\n")
        click.echo("waiting for the browser redirect… (Ctrl-C to cancel)")

    def on_prompt(arg):
        # The two flows report differently: device code hands back the whole
        # authorization response, loopback hands back a URL string.
        (on_url_prompt if isinstance(arg, str) else on_device_prompt)(arg)

    # §7.4: the terms-of-service position is stated BEFORE the flow runs, not
    # buried in docs. Several of these rows authenticate with a vendor CLI's
    # own OAuth client id, and the exposure lands on the account holder.
    note = getattr(spec, "tos_note", None)
    if note:
        click.echo(click.style("\nBefore you connect:", bold=True))
        click.echo(f"  {note}")
        if not click.confirm("Continue?", default=False):
            click.echo("cancelled — nothing was stored")
            return

    def read_code() -> str:
        # The manual-paste flow: the provider's callback page shows a code (some
        # render it as "<code>#<state>"). hide_input — it is a one-shot
        # credential and shoulder-surfing it is enough to steal the grant.
        return click.prompt("Paste the code from the browser", hide_input=True,
                            default="", show_default=False)

    try:
        result = connect(provider, _oauth_for(provider), on_prompt=on_prompt,
                         read_code=read_code, open_browser=_open_browser)
    except FlowError as exc:
        raise click.ClickException(str(exc))
    except KeyboardInterrupt:
        raise click.ClickException("cancelled — nothing was stored")

    click.echo(click.style(f"connected {provider}", fg="green")
               + f" ({_fmt_expiry(result.expires_at)}"
               + (", refreshable" if result.refresh_token else ", no refresh token")
               + ")")
    if not result.refresh_token:
        click.echo("note: no refresh token was issued — you will have to run "
                   "`polyrob auth add` again when it expires.")


@auth.command("remove")
@click.argument("provider")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip the confirmation.")
def remove_cmd(provider, yes):
    """Forget PROVIDER's stored credential (does NOT revoke it upstream)."""
    from core.llm_auth.flows import disconnect

    if not yes and not click.confirm(f"Remove the stored credential for {provider}?"):
        click.echo("cancelled")
        return
    removed = disconnect(provider)
    if not removed:
        click.echo(f"no stored credential for {provider}")
        return
    click.echo(f"removed {provider} from the auth store")
    # Saying this matters: a user who believes `remove` revoked upstream access
    # will not go turn it off, and the grant stays live.
    click.echo("this only forgot the local copy — revoke the grant in your "
               "provider account settings if you also want it dead upstream.")


@auth.command("refresh")
@click.argument("provider")
def refresh_cmd(provider):
    """Refresh PROVIDER's token now if it is at or near expiry."""
    from core.llm_auth.flows import FlowError, refresh_if_needed

    try:
        result = refresh_if_needed(provider, _oauth_for(provider))
    except FlowError as exc:
        raise click.ClickException(str(exc))
    if result is None:
        click.echo(f"{provider}: no refresh needed (or no refreshable credential stored)")
        return
    click.echo(f"{provider}: refreshed ({_fmt_expiry(result.expires_at)})")
