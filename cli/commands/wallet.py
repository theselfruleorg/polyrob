"""`polyrob wallet` — show the agent wallet: per-venue addresses, on-chain
balances, network, caps, and which venue is OPERATIONAL (the one funded + spent from).

This closes the interface gap that produced the 2026-07-08 fund-the-wrong-address
incident: the owner had no single place to see "what's my address / balance / which
one do I fund". Balances are best-effort over public RPCs (fail-open to n/a).

`polyrob wallet set-cap <daily|per-tx> <usd>` (owner-UX P2 T7) is the guided,
confirmed way to raise/lower a spend cap. C70/C69: this used to say it wrote
"the two money-authoritative env caps" and nothing else, which stopped being
the whole truth once the PolicyGate began re-resolving the ``budget.wallet_*``
preferences LIVE. It now writes the PREFERENCE by default (applies with no
restart, and the effective value is read back and printed), and `--env` writes
the operator envelope (`WALLET_DAILY_CAP_USD` / `AGENT_WALLET_MAX_PER_TX_USD`)
— which is what a RAISE of the daily cap, and disabling it, still require.
"""
from __future__ import annotations

import json as _json
import math
import os
from pathlib import Path

import click

from cli.commands.config import _upsert_env
from cli._admin_home import as_root_option
from core.paths import polyrob_home
from core.wallet.onchain import VENUE_CHAIN as _VENUE_CHAIN, balances as _balances
# C60: the venue rule lived here AND in `surfaces/telegram/owner_ops.py`, each
# with its own comment explaining the same footgun. One tuple, imported.
from surfaces.telegram.owner_ops import _FUNDABLE_VENUES

#: Venues that hold a same-chain float the agent spends directly. hyperliquid
#: (delegated signer, collateral in the master account) and polymarket (per-user
#: proxy creds) NEVER hold funds at their derived address.
_FUNDABLE = set(_FUNDABLE_VENUES)

# kind -> the env var it writes. Both are read directly by
# core.wallet.config.load_wallet_config (env-authoritative).
_CAP_ENV_KEY = {
    "daily": "WALLET_DAILY_CAP_USD",
    "per-tx": "AGENT_WALLET_MAX_PER_TX_USD",
}

# core.wallet.config.effective_daily_cap_usd / effective_max_per_tx_usd (the
# pref/env merge helpers) are real callers wired into load_wallet_config()
# -> PolicyGate (owner-UX G-13), and since 2026-09-18 re-resolved LIVE by the
# gate. The daily cap is min-merged: budget.wallet_daily_usd can TIGHTEN it,
# never raise or disable it. The per-tx ceiling is owner-override: an approved
# budget.wallet_per_tx_usd replaces the env default either way, clamped to the
# daily cap. Verify wiring: `grep -rn "effective_daily_cap_usd\|effective_max_per_tx_usd"
# core/ modules/ tools/ | grep -v test`.
_POLICY_GATE_CAVEAT = (
    "note: the daily cap is min-merged with the budget.wallet_daily_usd preference "
    "(tighten only); the per-tx ceiling is replaced by an owner-approved "
    "budget.wallet_per_tx_usd, clamped to the daily cap. Both apply live."
)



def _is_malformed_number(raw, key: str) -> bool:
    """True iff ``raw`` is a value ``load_wallet_config``'s own parser for
    ``key`` would reject. Delegates to the REAL parsers (H3, 2026-08-22:
    ``_cap_float`` for ``WALLET_DAILY_CAP_USD`` — disable-sentinel-aware, e.g.
    ``none``/``off`` — vs ``_req_float`` for ``AGENT_WALLET_MAX_PER_TX_USD``,
    which has no sentinel) so this can never diverge from what actually gets
    accepted. Used to NAME the offending cap env var (M12) when
    load_wallet_config's own parse raises. An unset/empty value is NOT
    malformed (it just falls back to the default), so only a present-but-
    garbage value trips this.
    """
    from core.wallet.config import _cap_float, _req_float, DEFAULT_DAILY_CAP_USD, DEFAULT_MAX_PER_TX_USD
    text = (raw or "").strip()
    if not text:
        return False
    probe_env = {key: raw}
    try:
        if key == "WALLET_DAILY_CAP_USD":
            _cap_float(probe_env, key, DEFAULT_DAILY_CAP_USD)
        else:
            _req_float(probe_env, key, DEFAULT_MAX_PER_TX_USD)
        return False
    except ValueError:
        return True


def _parse_positive_usd(raw: str) -> float:
    """Validate a cap amount: a positive, finite number.

    A cap of 0 is deliberately NOT accepted as "disabled" — that ambiguity
    (0 == disabled vs. 0 == "spend nothing") is exactly the kind of footgun
    this guided command exists to avoid. Disabling a cap is an explicit,
    separate VALUE (see `_parse_cap_arg` for the daily-only disable sentinel);
    it is no longer "remove the env var" (H3, 2026-08-22: WALLET_DAILY_CAP_USD
    now has a finite default, so an absent env var means the default, not
    disabled).
    """
    text = (raw or "").strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise click.ClickException(f"invalid amount {raw!r}: must be a number")
    if math.isnan(value) or math.isinf(value):
        raise click.ClickException(f"invalid amount {raw!r}: must be a finite number")
    if value <= 0:
        raise click.ClickException(
            f"invalid amount {raw!r}: a cap must be a positive number "
            "(to disable the daily cap, use `polyrob wallet set-cap daily none`)"
        )
    return value


def _parse_cap_arg(kind: str, raw: str) -> str:
    """Validate a `set-cap` argument for *kind*; returns the exact string to
    write to the env file.

    `per-tx` (AGENT_WALLET_MAX_PER_TX_USD) has no disable sentinel — must
    always be a positive finite number. `daily` (WALLET_DAILY_CAP_USD)
    additionally accepts an explicit disable word (H3, 2026-08-22: the same
    sentinel set `WALLET_DAILY_CAP_USD`'s own parser accepts —
    ``core.wallet.config._CAP_DISABLED``) — this is now the ONLY way to
    disable the aggregate cap, since an absent env var means the finite
    default, not "no cap".
    """
    from core.wallet.config import _CAP_DISABLED
    text = (raw or "").strip()
    if kind == "daily" and text.lower() in _CAP_DISABLED:
        return text.lower()
    _parse_positive_usd(raw)  # validates; raises click.ClickException on failure
    return text  # write the raw text verbatim (byte-identical to the old behavior)


@click.group("wallet", invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--no-balances", is_flag=True, help="Skip the on-chain balance lookups (offline/fast).")
@click.pass_context
def wallet_cmd(ctx: click.Context, as_json: bool, no_balances: bool):
    """Show the agent wallet: addresses, balances, network, caps, operational venue."""
    # C2 (2026-07-15): bootstrap the local env BEFORE any subcommand reads the wallet.
    # `wallet init` writes AGENT_WALLET_ENABLED/MASTER_SEED to ~/.polyrob/.env; without
    # this the bare view (and set-cap) never read that file, so they report "not
    # enabled" for a wallet `doctor` (which does load env) confirms. Mirrors owner.py
    # (unconditional in the group callback so every wallet subcommand sees file-set
    # config); export/init also load it themselves (override=False → harmless double).
    from core.bootstrap import load_env
    try:
        load_env(local_mode=True)
    except Exception:
        pass
    if ctx.invoked_subcommand is not None:
        return
    from core.wallet.factory import get_agent_wallet
    try:
        w = get_agent_wallet()
    except ValueError as e:
        # AgentWallet.__init__ raises ValueError only for a missing/short seed;
        # but load_wallet_config() (called first, inside the same try) can ALSO
        # raise ValueError from a malformed numeric env (e.g. a non-numeric
        # AGENT_WALLET_MAX_PER_TX_USD/WALLET_DAILY_CAP_USD) — a wholly different
        # problem that this handler used to mislabel as "seed missing/short"
        # (Finding 3, 2026-07-14 final review).
        #
        # M12 (2026-07-15): check AGENT_WALLET_ENABLED FIRST — a *disabled* wallet
        # must say "not enabled", never "ENABLED but …" (the malformed-cap config
        # raise happens before enabled/seed are ever read, so without this the
        # disabled+bad-cap case printed two lies). And NAME the malformed key in
        # the config-error branch instead of dumping a bare ValueError.
        from core.env import bool_env
        enabled = bool_env("AGENT_WALLET_ENABLED", False)
        bad_caps = [k for k in ("AGENT_WALLET_MAX_PER_TX_USD", "WALLET_DAILY_CAP_USD")
                    if _is_malformed_number(os.environ.get(k), k)]
        if not enabled:
            hint = (f" (note: {', '.join(bad_caps)} is not a number — fix that too)"
                    if bad_caps else "")
            msg = "agent wallet not enabled (set AGENT_WALLET_ENABLED=true)" + hint
            click.echo(_json.dumps({"enabled": False, "error": msg}) if as_json else msg)
            return
        if bad_caps:
            msg = (f"agent wallet config error: {', '.join(bad_caps)} is not a number "
                   f"— fix it in ~/.polyrob/.env (or `polyrob wallet set-cap`) [{e}]")
        else:
            seed = (os.environ.get("AGENT_WALLET_MASTER_SEED") or "").strip()
            if not seed or len(seed) < 32:
                msg = ("agent wallet is ENABLED but AGENT_WALLET_MASTER_SEED is missing/short — "
                       "run `polyrob wallet init` (or set the seed) to fix")
            else:
                msg = f"agent wallet config error: {e}"
        click.echo(_json.dumps({"enabled": True, "error": msg}) if as_json else msg)
        return
    if w is None:
        msg = "agent wallet not enabled (set AGENT_WALLET_ENABLED=true)"
        click.echo(_json.dumps({"enabled": False, "error": msg}) if as_json else msg)
        return

    cfg = w.config
    op = w.operational_venue
    # H14d: key derivation is LAZY (signer_for / w.address) and can raise ValueError
    # for an invalid bip44 seed or a scheme mismatch — get_agent_wallet() succeeds
    # (config valid, seed >=32) but the first address access fails. Wrap it so the
    # view shows a friendly MISCONFIGURED line instead of dumping a raw traceback.
    try:
        venues = []
        for venue in ("treasury", "x402", "hyperliquid", "polymarket"):
            addr = w.signer_for(venue).address
            chain = _VENUE_CHAIN[venue]
            fundable = venue in _FUNDABLE
            row = {"venue": venue, "address": addr, "chain": chain,
                   "operational": venue == op, "fundable": fundable}
            if not fundable:
                # delegated/managed elsewhere — never fund this derived address directly
                row["note"] = "delegated signer — not funded here"
            if fundable and not no_balances and cfg.network == "mainnet":
                native, usdc = _balances(addr, chain)
                row["usdc"] = usdc
                row["native"] = native
            venues.append(row)
        wallet_address = w.address
        # The SECOND address family off the same seed (2026-08-27): the
        # operator must be able to see — and fund — the Solana address from
        # the same view, or it stays invisible until a trade fails.
        # C30: a raising derivation used to render EXACTLY like a wallet with no
        # Solana account at all — the line simply vanished, so an owner funding
        # from this view had no way to tell "absent" from "broken".
        solana_address, solana_error = None, None
        try:
            solana_address = w.solana_address
        except Exception as exc:
            solana_error = f"{type(exc).__name__}: {exc}"
    except ValueError as e:
        msg = (f"agent wallet MISCONFIGURED: {e} "
               "(fix AGENT_WALLET_MASTER_SEED/AGENT_WALLET_DERIVATION or re-run `polyrob wallet init`)")
        click.echo(_json.dumps({"enabled": True, "error": msg}) if as_json else click.style(msg, fg="red"))
        return

    caps = {
        "max_per_tx_usd": cfg.max_per_tx_usd,
        "daily_cap_usd": cfg.daily_cap_usd,
        "per_venue_daily_cap_usd": cfg.per_venue_daily_cap_usd,
    }
    payload = {"enabled": True, "network": cfg.network, "operational_venue": op,
               "address": wallet_address, "solana_address": solana_address,
               "solana_error": solana_error, "venues": venues, "caps": caps}

    if as_json:
        click.echo(_json.dumps(payload, indent=2))
        return

    from core.wallet import derivation as _derivation
    click.echo(f"Agent wallet · network={cfg.network} · operational venue={op} "
               f"· derivation={_derivation.resolve_scheme()}")
    click.echo(f"Fund THIS address (operational): {wallet_address}")
    if cfg.network != "mainnet":
        click.echo(click.style("  ⚠ network is not mainnet — on-chain balances not shown.", fg="yellow"))
    click.echo("")
    for r in venues:
        star = " ←FUND" if r["operational"] else ""
        line = f"  {r['venue']:11s} {r['address']}  [{r['chain']}]{star}"
        if "usdc" in r:
            u = f"{r['usdc']:.2f}" if r["usdc"] is not None else "n/a"
            n = f"{r['native']:.5f}" if r["native"] is not None else "n/a"
            line += f"   USDC={u} gas={n}"
        elif r.get("note"):
            line += click.style(f"   ({r['note']})", fg="yellow")
        click.echo(line)
    if solana_error:
        click.echo(click.style(
            f"  {'solana':11s} UNREADABLE — the Solana account could not be "
            f"derived ({solana_error}). This is NOT 'no Solana wallet': do not "
            f"assume the address is absent.", fg="yellow"))
    elif solana_address:
        sol_line = f"  {'solana':11s} {solana_address}  [solana] ←FUND for Solana trades"
        if not no_balances and cfg.network == "mainnet":
            # C30: the balance probe swallowed every failure, so an RPC outage
            # printed a bare address that read as "funded, amount not shown".
            try:
                from core.wallet import solana_onchain
                sol_bal = solana_onchain.native_balance(solana_address)
                usdc_raw = None
                from core.wallet import chains as _chains
                _sol_row = _chains.get("solana")
                if _sol_row and _sol_row.usdc:
                    _tb = solana_onchain.token_balances(solana_address)
                    usdc_raw = None if _tb is None else _tb.get(_sol_row.usdc, 0)
                s = f"{sol_bal:.5f}" if sol_bal is not None else "n/a"
                u = f"{usdc_raw / 1e6:.2f}" if usdc_raw is not None else "n/a"
                sol_line += f"   USDC={u} SOL={s}"
            except Exception as exc:
                sol_line += click.style(
                    f"   balances UNREAD ({type(exc).__name__}) — not zero",
                    fg="yellow")
        click.echo(sol_line)
    click.echo("")
    dc = f"${caps['daily_cap_usd']:.2f}" if caps["daily_cap_usd"] is not None else "none"
    click.echo(f"Caps: max ${caps['max_per_tx_usd']:.2f}/tx · daily {dc}"
               + (f" · per-venue {caps['per_venue_daily_cap_usd']}" if caps["per_venue_daily_cap_usd"] else ""))
    # M13 (2026-07-15): the "catastrophic ceiling, NOT a budget" distinction used
    # to live only in code comments — surface it. And "daily none" is UNLIMITED
    # (no rolling-24h limit), which is the real posture a new owner never sees.
    if caps["daily_cap_usd"] is None:
        click.echo(click.style(
            "  ⚠ per-tx is a catastrophic-loss CEILING, not a budget; "
            "daily cap is UNLIMITED (no rolling-24h limit).", fg="yellow"))
        click.echo("    Set a real daily budget:  polyrob wallet set-cap daily <usd>")
    else:
        click.echo(click.style(
            "  note: the per-tx cap is a catastrophic-loss CEILING, not a budget "
            "— the daily cap is your real budget.", fg="yellow"))


@wallet_cmd.command("set-cap")
@click.argument("kind", type=click.Choice(sorted(_CAP_ENV_KEY)))
@click.argument("usd")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Skip the confirmation prompt (non-interactive use).")
@click.option("--env", "write_env", is_flag=True, default=False,
              help="Write the OPERATOR env value instead of the owner "
                   "preference (needs a restart; the only way to disable the "
                   "daily cap or to RAISE the operator envelope).")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the global env-file home (test/ops only).")
@as_root_option
def set_cap_cmd(kind: str, usd: str, yes: bool, write_env: bool,
                home_dir_opt: str | None):
    """Set the wallet's DAILY or PER-TX USD spend cap.

    By default this writes the OWNER PREFERENCE the live gate re-reads
    (`budget.wallet_daily_usd` / `budget.wallet_per_tx_usd`), so it applies
    immediately with no restart, and the effective value is read back and
    printed. `--env` writes the operator env var instead — needed to RAISE the
    daily envelope (the pref is min-merged and can only tighten) and to
    disable the daily cap (`set-cap daily none`).
    """
    value_to_write = _parse_cap_arg(kind, usd)
    key = _CAP_ENV_KEY[kind]
    from core.wallet.config import _CAP_DISABLED as _DISABLE_WORDS
    disabling = kind == "daily" and value_to_write in _DISABLE_WORDS
    if not write_env and disabling:
        raise click.ClickException(
            "a preference cannot DISABLE the daily cap — it is min-merged, so "
            "it may only tighten. Disabling the operator envelope is an env "
            "write:\n    polyrob wallet set-cap daily none --env")
    if not write_env:
        _set_cap_pref(kind, float(value_to_write), yes=yes)
        return
    home = Path(home_dir_opt) if home_dir_opt else polyrob_home()
    path = home / ".env"
    line = f"{key}={value_to_write}"

    click.echo(f"About to write to {path}:")
    click.echo(f"  {line}")
    if not yes and not click.confirm("Proceed?", default=False):
        click.echo("Aborted — no changes written.")
        return

    _upsert_env(path, key, value_to_write, secure=True)

    if disabling:
        click.echo(f"Wrote {key}={value_to_write} to {path} "
                   "(daily cap DISABLED — unbounded aggregate spend).")
    else:
        label = "daily cap" if kind == "daily" else "per-transaction cap"
        click.echo(f"Wrote {key}={value_to_write} to {path} ({label}).")
    # L10 (2026-07-15): "restart" alone is ambiguous — name WHICH process re-reads
    # WHICH env file. The local `polyrob` CLI/REPL reads this global ~/.polyrob/.env
    # at startup; a systemd service reads its OWN env file (prod: /etc/polyrob/
    # polyrob.env), so on a server deploy the cap must be set there instead.
    click.echo(f"Takes effect on restart: the local `polyrob` CLI/REPL re-reads "
               f"{path} at startup.")
    click.echo("  (A systemd deploy reads its own env file — e.g. "
               "/etc/polyrob/polyrob.env — set the cap there and restart the service.)")
    click.echo(_POLICY_GATE_CAVEAT)


def _set_cap_pref(kind: str, usd: float, *, yes: bool) -> None:
    """C69: write the SAME ``budget.wallet_*`` preference the gate reads.

    ``set-cap`` wrote ``~/.polyrob/.env`` and said "takes effect on restart",
    while the PolicyGate re-resolves ``budget.wallet_daily_usd`` /
    ``budget.wallet_per_tx_usd`` LIVE — two writers for one number, and the one
    the CLI used was the one a running service does not re-read. This writes
    the live one and then READS BACK what the gate would now enforce, so the
    confirmation is a measurement rather than a promise (the daily pref is
    min-merged, so a raise above the operator envelope changes nothing and the
    read-back is what says so).
    """
    from core import prefs
    from core.admin_data_home import AmbiguousDataHome, admin_owner_principal
    from cli._admin_home import admin_data_dir
    from core.wallet.config import effective_daily_cap_usd, effective_max_per_tx_usd

    pref_key = ("budget.wallet_daily_usd" if kind == "daily"
                else "budget.wallet_per_tx_usd")
    try:
        home_dir, tenant = admin_data_dir(write=True), admin_owner_principal()
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))

    click.echo(f"About to set {pref_key} = {usd:g} for tenant {tenant}")
    if not yes and not click.confirm("Proceed?", default=False):
        click.echo("Aborted — no changes written.")
        return
    ok, err = prefs.write_preference(home_dir, tenant, pref_key, float(usd))
    if not ok:
        raise click.ClickException(err)

    if kind == "daily":
        effective = effective_daily_cap_usd(tenant, home_dir)
        shown = "disabled" if effective is None else f"${effective:.2f}"
    else:
        effective = effective_max_per_tx_usd(tenant, home_dir)
        shown = f"${effective:.2f}"
    label = "daily cap" if kind == "daily" else "per-transaction cap"
    click.echo(click.style(
        f"Wrote the {label} preference ({pref_key}={usd:g}); it applies LIVE — "
        f"no restart.", fg="green"))
    click.echo(f"Effective {label} now: {shown}")
    if effective is not None and abs(float(effective) - float(usd)) > 1e-9:
        if kind == "daily":
            click.echo(click.style(
                "  ⚠ the preference did NOT take the value you asked for: the "
                "daily cap is min-merged with the operator env value, so it can "
                "only TIGHTEN. To raise the envelope:\n"
                "      polyrob wallet set-cap daily <usd> --env    (then restart)",
                fg="yellow"))
        else:
            click.echo(click.style(
                "  ⚠ the per-transaction ceiling is clamped to the daily cap, so "
                "it stopped below what you asked for. Raise the daily cap first.",
                fg="yellow"))
    click.echo(_POLICY_GATE_CAVEAT)


def run_wallet_init_flow(*, mnemonic, raw_seed, home, assume_yes, data_dir=None):
    """Create (or import) the agent wallet in one step. Returns a summary dict.

    - no args      -> generate a fresh 24-word BIP-39 mnemonic (bip44 scheme)
    - mnemonic=... -> import an existing mnemonic (bip44 scheme)
    - raw_seed=... -> import a legacy raw seed (legacy scheme — migrating an
                      older install keeps its addresses)
    Writes AGENT_WALLET_ENABLED/MASTER_SEED to <home>/.env (chmod 600) and
    records the derivation scheme write-once. Refuses if a seed is already set.
    """
    import os
    import sys
    from pathlib import Path as _P
    from core.wallet import derivation
    from core.wallet.signer import LocalEoaSigner

    if (os.environ.get("AGENT_WALLET_MASTER_SEED") or "").strip():
        raise click.ClickException(
            "a wallet seed is already configured (AGENT_WALLET_MASTER_SEED is set).\n"
            "To see it: polyrob wallet export.  To replace it, remove the env var first "
            "(DANGER: the old addresses keep any funds — export/back up first).")

    generated = False
    if mnemonic:
        seed, scheme = mnemonic.strip(), "bip44"
        if not derivation.is_valid_mnemonic(seed):
            raise click.ClickException("that is not a valid BIP-39 mnemonic")
    elif raw_seed:
        seed, scheme = raw_seed.strip(), "legacy"
        if len(seed) < 32:
            raise click.ClickException("raw seed must be >= 32 chars (this is the legacy import path)")
    else:
        try:
            from eth_account import Account
        except ImportError:
            raise click.ClickException(
                "wallet generation needs the crypto extra: pip install 'polyrob[crypto]'")
        Account.enable_unaudited_hdwallet_features()
        _acct, seed = Account.create_with_mnemonic(num_words=24)
        scheme, generated = "bip44", True

    address = LocalEoaSigner(derivation.derive_key(seed, "treasury", scheme)).address

    # Record the derivation scheme FIRST (write-once meta.json) — before the
    # seed is persisted anywhere or the address is printed. If this raises
    # (conflicting existing meta, e.g. a prior "legacy" install), NOTHING has
    # been written yet: a clean abort, not a persisted seed whose printed
    # address doesn't match the scheme the runtime will later resolve
    # (Finding 2, 2026-07-14 final review).
    derivation.write_scheme_once(scheme, data_dir=data_dir)

    env_path = _P(home) / ".env"
    _upsert_env(env_path, "AGENT_WALLET_ENABLED", "true", secure=True)
    _upsert_env(env_path, "AGENT_WALLET_MASTER_SEED", seed, secure=True)
    # H1 (2026-07-15): pin the derivation scheme in the SAME global .env as the seed
    # so the scheme travels WITH the seed and resolve_scheme (env override wins) is
    # CWD-independent. The write-once meta.json lives under the data-home (CWD-relative
    # in local mode when POLYROB_DATA_DIR is unset), so running from a different dir
    # could miss it and silently flip a funded bip44 wallet to legacy = wrong address.
    _upsert_env(env_path, "AGENT_WALLET_DERIVATION", scheme, secure=True)
    os.environ["AGENT_WALLET_MASTER_SEED"] = seed
    os.environ["AGENT_WALLET_ENABLED"] = "true"
    os.environ["AGENT_WALLET_DERIVATION"] = scheme

    if generated:
        # L2 (2026-07-15): NEVER write the generated mnemonic to a non-TTY stdout
        # (`polyrob wallet init --yes > setup.log` would persist secret material to
        # a file/pipe). Redact and point at the interactive reveal instead.
        if sys.stdout.isatty():
            click.echo("\nYour wallet mnemonic (shown ONCE here — write it down and back it up):\n")
            click.echo(click.style(f"  {seed}\n", bold=True))
            click.echo("Anyone with these words controls the funds. polyrob will only show "
                       "them again via `polyrob wallet export`.\n")
        else:
            click.echo("\nA new wallet mnemonic was generated but NOT printed — stdout is "
                       "not a TTY, and secret material must never be written to a file/pipe.")
            click.echo("Reveal it interactively (TTY only): polyrob wallet export\n")
    click.echo(f"Fund THIS address (treasury, {scheme}): {address}")
    network = (os.environ.get("AGENT_WALLET_NETWORK") or "testnet").strip().lower()
    if network != "mainnet":
        click.echo("network=testnet (default) — use a Base-Sepolia faucet for test USDC; "
                   "switch with `polyrob config set AGENT_WALLET_NETWORK mainnet`.")
    else:
        click.echo("network=mainnet — fund with USDC on Base.")

    # M13 (2026-07-15): surface the REAL spend posture at init — the per-tx
    # cap is a catastrophic-loss ceiling, not a budget. H3 (2026-08-22): both
    # caps now resolve through the SAME parsers load_wallet_config() uses
    # (never a second, divergent "1000"/blank-means-unlimited display parser),
    # so a malformed pre-existing env var is reported here too, not silently
    # shown as if it were the default.
    from core.wallet.config import _cap_float, _req_float, DEFAULT_DAILY_CAP_USD, DEFAULT_MAX_PER_TX_USD
    try:
        max_tx = _req_float(os.environ, "AGENT_WALLET_MAX_PER_TX_USD", DEFAULT_MAX_PER_TX_USD)
        daily = _cap_float(os.environ, "WALLET_DAILY_CAP_USD", DEFAULT_DAILY_CAP_USD)
    except ValueError as e:
        raise click.ClickException(f"wallet caps misconfigured: {e}")
    if daily is not None:
        click.echo(f"Spend caps: ${max_tx:.2f}/tx ceiling · ${daily:.2f}/day budget.")
    else:
        click.echo(f"Spend caps: ${max_tx:.2f}/tx (a catastrophic-loss CEILING, not a budget) "
                   "· daily UNLIMITED.")
        click.echo("Set a real daily budget:  polyrob wallet set-cap daily <usd>")

    linked = False
    if not (os.environ.get("X402_PAYMENT_RECIPIENT") or "").strip():
        if assume_yes or click.confirm(
                "Point earnings (X402_PAYMENT_RECIPIENT) at this wallet so invoices "
                "settle to an address the agent can spend from?", default=True):
            _upsert_env(env_path, "X402_PAYMENT_RECIPIENT", address, secure=True)
            os.environ["X402_PAYMENT_RECIPIENT"] = address
            linked = True
    # M15 (2026-07-15): ALWAYS echo the money-routing write — under --yes the confirm
    # is skipped, so without this the X402_PAYMENT_RECIPIENT write was silent.
    if linked:
        click.echo(f"Linked earnings: wrote X402_PAYMENT_RECIPIENT={address} "
                   "(invoices settle to this wallet).")
    click.echo("Takes effect: restart any running polyrob process.")
    return {"address": address, "scheme": scheme, "env_path": str(env_path),
            "linked_recipient": linked}


@wallet_cmd.command("export")
@click.option("--venue", default=None,
              type=click.Choice(sorted(["treasury", "x402", "polymarket", "hyperliquid"])),
              help="Export only this venue's private key (default: all + mnemonic if bip44).")
def wallet_export_cmd(venue):
    """Reveal the wallet's seed/mnemonic and per-venue private keys (DANGEROUS).

    TTY-only with a typed confirmation. Each venue key is a standard secp256k1
    private key importable into MetaMask/Rabby as an account. NEVER available
    to the agent — this command exists only on the operator CLI.
    """
    import os, sys
    from core.bootstrap import load_env
    try:
        load_env(local_mode=True)
    except Exception:
        pass
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise click.ClickException("export is interactive-only (needs a TTY; refusing piped output)")
    seed = (os.environ.get("AGENT_WALLET_MASTER_SEED") or "").strip()
    if not seed:
        # 2026-09-14: on a systemd deployment the seed lives in the units' env
        # files (`/etc/polyrob/wallet.env`, then the legacy `polyrob.env`), which
        # the operator CLI does not load. Read them here — root-readable only —
        # so the owner can export without sourcing the files by hand. The file
        # is opened, never echoed.
        seed = _seed_from_system_env_files()
    if not seed:
        raise click.ClickException(
            "no wallet configured — run `polyrob wallet init` first (on a server: the seed "
            "is read from /etc/polyrob/wallet.env or /etc/polyrob/polyrob.env; run as root)")

    click.echo("This prints PRIVATE key material. Anyone who sees it controls the funds.")
    typed = click.prompt("Type EXPORT to continue", default="", show_default=False)
    if typed.strip() != "EXPORT":
        raise click.ClickException("aborted — nothing exported")

    from core.wallet import derivation
    # H14d: resolve the scheme and derive ALL keys BEFORE printing anything, so a
    # misconfigured wallet (invalid bip44 seed, corrupt meta.json) fails cleanly with a
    # friendly error — never dumps a raw traceback AFTER already printing the mnemonic.
    try:
        scheme = derivation.resolve_scheme()
        venues = [venue] if venue else sorted(derivation.VENUE_INDEX)
        derived = [(v, derivation.derive_key(seed, v, scheme)) for v in venues]
    except ValueError as e:
        raise click.ClickException(
            f"wallet MISCONFIGURED — cannot export: {e}. Fix the seed/derivation first "
            "(nothing was printed).")
    click.echo(f"\nderivation: {scheme}")
    if scheme == "bip44" and not venue:
        click.echo("mnemonic (BIP-39 — imports into any standard wallet; "
                   "account 0 == treasury):")
        click.echo(click.style(f"  {seed}", bold=True))
    click.echo("\nper-venue private keys (secp256k1 hex — import as single accounts):")
    for v, key in derived:
        click.echo(f"  {v:11s} 0x{key.hex()}")
    # Solana (2026-09-14): the SAME seed also derives the agent's Solana account
    # (SLIP-0010, m/44'/501'/0'/0'), which the EVM keys above cannot reach. Print
    # it in the form Phantom/Solflare/Backpack import ("private key" = base58 of
    # the 64-byte keypair) and the byte-array form solana-cli reads.
    if not venue:
        try:
            from core.wallet.solana_signer import derive_solana_keypair
            kp = derive_solana_keypair(seed, 0)
            click.echo("\nsolana account 0 (ed25519 — Phantom/Solflare 'import private key'):")
            click.echo(f"  address     {kp.pubkey()}")
            click.echo(f"  private key {kp}")
            click.echo(f"  solana-cli  {list(bytes(kp))}")
        except Exception as e:  # noqa: BLE001 — solders absent → say so, never hide
            click.echo(f"\nsolana key: unavailable ({e}) — install `solders` to export it")
    click.echo("\n⚠ Clear your terminal scrollback/shell history after copying "
               "(this output is exactly as sensitive as the funds).")


#: The systemd env files the operator export may read (first hit wins).
_SYSTEM_ENV_FILES = ("/etc/polyrob/wallet.env", "/etc/polyrob/polyrob.env")


def _seed_from_system_env_files() -> str:
    """The seed from the systemd env files, when this process may read them."""
    from pathlib import Path
    for p in (Path(x) for x in _SYSTEM_ENV_FILES):
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("AGENT_WALLET_MASTER_SEED="):
                    v = line.split("=", 1)[1].strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                        v = v[1:-1]
                    if v:
                        return v
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return ""


@wallet_cmd.command("init")
@click.option("--from-mnemonic", "mnemonic", default=None,
              help="Import an existing BIP-39 mnemonic instead of generating one.")
@click.option("--from-seed", "raw_seed", default=None,
              help="Import a legacy raw seed (>=32 chars) — keeps a pre-BIP44 install's addresses.")
@click.option("--yes", "-y", is_flag=True, default=False, help="No prompts (accept defaults).")
@click.option("--data-dir", "data_dir_opt", default=None, hidden=True,
              help="Override the wallet meta dir (test/ops only).")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the global env-file home (test/ops only).")
@as_root_option
def wallet_init_cmd(mnemonic, raw_seed, yes, data_dir_opt, home_dir_opt):
    """Create the agent's wallet in one command (or import an existing one).

    Pass ``--from-mnemonic`` / ``--from-seed`` with an EMPTY value (e.g.
    ``--from-mnemonic ""``) to be prompted for the secret with a HIDDEN prompt,
    keeping it off the shell history and `ps` output (M14).
    """
    from pathlib import Path as _P
    from core.bootstrap import load_env
    try:
        load_env(local_mode=True)  # see file-based seeds before deciding "already set"
    except Exception:
        pass
    # M14 (2026-07-15): a flag supplied with an EMPTY value ("") is the request for
    # a hidden prompt — so the master secret is never placed on the command line
    # (shell history + `ps`). ``None`` = flag absent (generate a fresh wallet);
    # ``""`` = flag present but empty (prompt). Only prompt on an interactive TTY.
    import sys
    if mnemonic == "":
        if not sys.stdin.isatty():
            raise click.ClickException(
                "--from-mnemonic given with no value and stdin is not a TTY "
                "(refusing to read a secret non-interactively)")
        mnemonic = click.prompt("Paste your BIP-39 mnemonic", hide_input=True).strip()
    if raw_seed == "":
        if not sys.stdin.isatty():
            raise click.ClickException(
                "--from-seed given with no value and stdin is not a TTY "
                "(refusing to read a secret non-interactively)")
        raw_seed = click.prompt("Paste your legacy raw seed", hide_input=True).strip()
    if mnemonic and raw_seed:
        raise click.ClickException("use --from-mnemonic OR --from-seed, not both")
    home = _P(home_dir_opt) if home_dir_opt else polyrob_home()
    # C8: the write-once derivation scheme (`meta.json`) went to whatever
    # `derivation.write_scheme_once` resolved on its own — in a shell with no
    # POLYROB_DATA_DIR that is a CWD-relative `.polyrob`, so the scheme the
    # RUNNING service later resolves was pinned in a tree it never reads, and a
    # bip44 wallet could silently resolve as legacy = a different address.
    if data_dir_opt:
        data_dir = _P(data_dir_opt)
    else:
        from cli._admin_home import admin_data_dir
        data_dir = _P(admin_data_dir(write=True))
    run_wallet_init_flow(mnemonic=mnemonic or None, raw_seed=raw_seed or None, home=home,
                         assume_yes=yes, data_dir=data_dir)


# ---------------------------------------------------------------------------
# 037 — cross-chain bridge (owner seat)
# ---------------------------------------------------------------------------

def _admin_home(*, write: "bool | None" = None) -> str:
    """The data home every money read/write on this seat acts on (the 031 rule)."""
    from cli._admin_home import admin_data_dir
    return admin_data_dir(write=write)


def _admin_tenant(user_id=None) -> str:
    """The tenant every money view on this seat scopes to — the ONE resolver."""
    if user_id:
        return user_id
    from core.admin_data_home import AmbiguousDataHome, admin_owner_principal
    try:
        return admin_owner_principal()
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))


def _bridge_chain_key(chain_id):
    """The registry key for a chain id ON A BRIDGE ROW, or ``None``.

    Two resolvers, because a bridge row carries two KINDS of id and only one of
    them is a chain identifier. ``bridge_guard.chain_name_for_id`` owns the
    registry ids (and the rule that a falsey id is NOT Solana, whose row carries
    ``chain_id=0`` because EIP-155 has no analogue). The other is the PROVIDER's
    own pseudo id for Solana, which is what ``record_pending`` stores as
    ``origin_chain_id`` for an SVM origin — a `tools`-tier constant that the
    core tier may not import, so it is resolved HERE, at the seat that renders
    the row, from the module that owns it rather than from a copy.
    """
    from core.wallet.bridge_guard import chain_name_for_id
    name = chain_name_for_id(chain_id)
    if name:
        return name
    try:
        from tools.defi.providers.relay_bridge import SOLANA_CHAIN_ID
        if int(chain_id) == int(SOLANA_CHAIN_ID):
            return "solana"
    except Exception:
        pass
    return None


def _chain_name(chain_id) -> str:
    """043 D1: a bridge row said "-> chain 4663". A chain id is not a place a
    person has been; the NAME is, and the id stays beside it so an operator can
    still match it against the registry."""
    name = _bridge_chain_key(chain_id)
    if not name:
        return f"chain {chain_id}"
    return f"{name} (chain {chain_id})"


def _tx_link(chain_id, tx_ref) -> str:
    """An explorer link for a transaction that LANDED on *chain_id*, or ``""``.

    ⚠️ ``tx_ref`` on a bridge row is the ORIGIN transaction
    (``bridge_verb`` settles ``tx_ref=signature`` on the send), so it must be
    resolved against the ORIGIN chain. Linking it to the destination put a
    Solana base58 signature inside an EVM explorer URL — a link that looks
    authoritative and resolves to nothing. Same rule as
    ``core.wallet.tx_notify``: a link only for the chain the transaction landed
    on, and no link at all when that chain cannot be named.
    """
    if not tx_ref:
        return ""
    from core.wallet.chains import explorer_url
    name = _bridge_chain_key(chain_id)
    if not name:
        return ""
    return explorer_url(name, "tx", str(tx_ref)) or ""


@wallet_cmd.command("book")
@click.option("--user", "user_id", default=None, help="Tenant id (default: this identity).")
def wallet_book(user_id):
    """The ledger against every money chain: one verdict, then what disagrees.

    The SAME reader the console's Money page and the REPL's `/book` use
    (`tools.defi.book.read_book`), through the SAME renderer
    (`core.surfaces.inbox_render.render_book`) — so no two seats can describe
    one book differently. Read-only: nothing signs, nothing broadcasts.

    A chain it could not read is UNVERIFIED, never clean.
    """
    import asyncio

    from core.surfaces.inbox_render import render_book
    from tools.defi.book import read_book

    # C10: `resolve_data_home()` never applies the server default, so in an SSH
    # shell with no POLYROB_DATA_DIR the book was read from a tree the service
    # never writes and rendered a clean, empty, WRONG book. Same seam as every
    # other owner verb.
    uid = _admin_tenant(user_id)
    try:
        body = asyncio.run(read_book(uid, _admin_home(write=False)))
    except Exception as exc:
        raise click.ClickException(
            f"the book could not be read ({exc}). That is UNKNOWN, not a clean "
            f"book — do not trade on it.")
    click.echo(render_book(body))


@wallet_cmd.command("bridges")
@click.option("--user", "user_id", default=None, help="Tenant id (default: this identity).")
def wallet_bridges(user_id):
    """Bridges that have not been proven to arrive.

    An `in_flight` row is an open question about YOUR money, not an error to
    dismiss: the send landed and the arrival was not measured before the
    deadline. Never re-send one — a re-sent bridge pays twice.
    """
    from core.wallet import bridge_guard

    # C9: same home rule as the book — an in-flight bridge is money in motion,
    # and reading the wrong store answers "none in flight" over a real one.
    uid = _admin_tenant(user_id)
    _admin_home(write=False)   # adopt/refuse the deployed home before reading
    try:
        rows = bridge_guard.open_bridges(uid)
    except Exception as exc:
        raise click.ClickException(
            f"the bridge store could not be read ({exc}). That is UNKNOWN, not "
            f"'no bridges in flight' — do not treat it as all-clear.")
    if not rows:
        click.echo("No bridges awaiting confirmation.")
        return
    click.echo(f"{len(rows)} bridge(s) awaiting confirmation:")
    import datetime as _dt
    for r in rows:
        # C61: `r["created_at"]` / `r["request_id"]` were unguarded index reads
        # and `utcfromtimestamp` is deprecated (3.12 warns) — one row missing a
        # stamp took the WHOLE in-flight listing down with a KeyError, and that
        # is the one listing that must never fail to print.
        try:
            when = _dt.datetime.fromtimestamp(
                float(r.get("created_at") or 0),
                tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        except (TypeError, ValueError):
            when = "time unknown"
        usd = "" if r.get("amount_usd") is None else f" ${float(r['amount_usd']):,.2f}"
        dest = _chain_name(r.get("dest_chain_id"))
        click.echo(f"  {r.get('id') or '(no id)'}  [{r.get('state') or '?'}]{usd}"
                   f"  -> {dest}  {when}")
        click.echo(f"     relay request: {r.get('request_id') or 'unknown'}")
        # The hash is the ORIGIN send, so the link is the ORIGIN explorer.
        link = _tx_link(r.get("origin_chain_id"), r.get("tx_ref"))
        if link:
            click.echo(f"     {link}")
        elif r.get("tx_ref"):
            click.echo(f"     tx {r['tx_ref']}")
        if r.get("detail"):
            click.echo(f"     {str(r['detail'])[:300]}")


@wallet_cmd.command("bridge")
@click.argument("from_chain")
@click.argument("to_chain")
@click.argument("amount", type=float)
@click.option("--execute", is_flag=True, default=False,
              help="Actually broadcast. Without it this is a quote + full "
                   "assertion pass that sends nothing.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the typed confirmation (for a non-interactive owner run).")
@click.option("--token-out", default="native", show_default=True,
              help="Destination asset: 'native', or a token PINNED in the chain "
                   "registry ('weth'/'usdc' where that chain has one). An "
                   "arbitrary address is refused. ⚠️ v1 bridges NATIVE→NATIVE: "
                   "anything else is refused by the verb, not silently swapped.")
@as_root_option
def wallet_bridge(from_chain, to_chain, amount, execute, yes, token_out):
    """Bridge NATIVE value between chains: polyrob wallet bridge solana robinhood 0.5

    THE OWNER SEAT, and since 2026-09-12 not the only one — a bridge runs on
    CAPS, NOT TAPS: under DEFI_AUTONOMOUS_MAX_USD the agent may run one itself
    and report; above it the durable owner queue decides. (The previous wording
    here, "a bridge never runs on an autonomous turn", described the superseded
    2026-09-11 policy.)

    It quotes through Relay, asserts the order against what was asked for,
    simulates the send, then — only with --execute — signs, broadcasts and PROVES
    THE ARRIVAL by measuring the destination balance.
    """
    import asyncio

    from tools.defi.bridge_verb import perform_bridge
    from tools.defi.trade_tool import BridgeParams, DefiTradeTool

    params = BridgeParams(from_chain=from_chain, to_chain=to_chain, amount=amount,
                          token_out=token_out, dry_run=not execute)
    tool = DefiTradeTool()
    # C63: the confirmation below said "bridge N native from -> to" regardless
    # of --token-out, so an operator who asked for usdc on the far side typed
    # "yes" to a sentence describing a different transaction.
    _asset = (token_out or "native").strip().lower()

    if execute and not yes:
        click.echo(click.style(
            f"About to bridge {amount} native {from_chain} -> {_asset} on "
            f"{to_chain}. This moves real funds and cannot be undone.",
            fg="yellow"))
        click.confirm("Proceed?", abort=True)

    # The CLI IS the owner seat, so there is no forged-turn context to pass.
    ctx = _owner_ctx()
    result = asyncio.run(perform_bridge(tool, params, ctx))
    if getattr(result, "error", None):
        raise click.ClickException(result.error)
    # ⚠️ ActionResult's field is `extracted_content`, NOT `content`. Reading
    # `.content` printed "(no output)" over a complete, correct bridge report on
    # the first prod run — the CLI silently swallowed everything the guard had
    # to say. The local test stub set `.content`, so it diverged from the real
    # type and never caught it: a stub that does not match its subject tests the
    # stub.
    body = getattr(result, "extracted_content", None)
    if not body:
        raise click.ClickException(
            "the bridge returned neither an error nor a report — that is a bug, "
            "not an empty result; nothing should be assumed about what happened")
    click.echo(body)


# ---------------------------------------------------------------------------
# 042 — token creation and the launchpad, on the same owner seat as `bridge`.
#
# Deliberately under `wallet` rather than as new top-level commands: the owner's
# 2026-09-13 interface decision cut the CLI from 45 aliases to 30 and holds
# there, and these belong to the wallet's authority anyway.
# ---------------------------------------------------------------------------

def _owner_ctx():
    """The CLI IS the owner seat, so there is no forged-turn context to pass.

    ⚠️ The tenant is ``_admin_tenant()`` — the deployed-env-aware resolver every
    money VIEW on this seat already uses — NOT the bare
    ``core.instance.resolve_owner_user_id``. On a deployed box systemd exports
    ``POLYROB_OWNER_USER_ID`` and an owner's SSH shell does not, so the two
    answer different tenants: the spend would be RECORDED under one bucket and
    the ledger, the caps and ``polyrob wallet book`` would read another. That is
    the 033 tenantless-``wallet_spend`` defect (a confident ``$0.00`` over 114
    real rows) in a new place. Off a deployed box the two are identical.
    """
    from types import SimpleNamespace
    return SimpleNamespace(user_id=_admin_tenant(), role="owner", is_sub_agent=False,
                           metadata={})


def _echo_result(result, *, what: str):
    if getattr(result, "error", None):
        raise click.ClickException(result.error)
    # ⚠️ ActionResult's field is `extracted_content`, NOT `content` — reading
    # `.content` printed "(no output)" over a complete bridge report on its
    # first prod run.
    body = getattr(result, "extracted_content", None)
    if not body:
        raise click.ClickException(
            f"the {what} returned neither an error nor a report — that is a bug, "
            f"not an empty result; nothing should be assumed about what happened")
    click.echo(body)


@wallet_cmd.command("deploy-token")
@click.argument("symbol")
@click.argument("supply", type=float)
@click.argument("name", nargs=-1, required=True)
@click.option("--chain", default="base", show_default=True)
@click.option("--decimals", type=int, default=None,
              help="Decimal places. Default 18 on an EVM chain (the ERC-20 "
                   "norm), 9 on Solana (SOL's own). Solana's ceiling is 9.")
@click.option("--max-usd", type=float, default=25.0, show_default=True,
              help="The most this deployment may cost. It sends nothing, so "
                   "this bounds the GAS FEE.")
@click.option("--vanity", default="",
              help="Mine an address starting with these HEX characters (0-9a-f, "
                   "max 8 — each one is 16x the work). Uses the deterministic "
                   "CREATE2 factory, so the address is the same on every chain.")
@click.option("--salt", default="",
              help="An explicit 32-byte CREATE2 salt. Same effect as --vanity "
                   "without the search.")
@click.option("--uri", default="",
              help="SOLANA only: URI of a JSON metadata file "
                   "({name,symbol,description,image}). This is where the LOGO "
                   "comes from; without it the token shows with no picture. "
                   "An EVM deploy REFUSES it rather than dropping it.")
@click.option("--execute", is_flag=True, default=False,
              help="Actually broadcast. Without it this simulates, asserts the "
                   "produced bytecode against the pinned template, and reports "
                   "the address it WOULD land at.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the typed confirmation.")
@as_root_option
def wallet_deploy_token(symbol, supply, name, chain, decimals, max_usd,
                        vanity, salt, uri, execute, yes):
    """Deploy a fixed-supply token: polyrob wallet deploy-token ROB 1e9 Rob Coin

    On an EVM chain it deploys ONE audited template whose runtime the guard
    compares byte for byte before signing — no mint function, no owner, no
    pause, no transfer fee, whole supply to the wallet.

    On `--chain solana` it mints a fixed-supply SPL token and REVOKES the mint
    authority in the same transaction, then reads the mint back and reports what
    is actually true of it. The name and symbol are written ON-CHAIN (Token-2022
    metadata, no Metaplex account); `--uri` points at a JSON file with the logo.

    `--vanity b0b` mines an address starting with those hex characters, through
    the deterministic factory — which also means the SAME address on every EVM
    chain for the same bytes.

    This does NOT make the token tradable — use `polyrob wallet launch` for that.
    """
    import asyncio

    from tools.defi.deploy_verb import perform_deploy_token
    from tools.defi.trade_tool import DefiTradeTool, DeployTokenParams

    label = " ".join(name)
    is_solana = str(chain).strip().lower() == "solana"

    # C35: `--decimals` defaulted to 18 and the Solana branch silently did
    # `min(decimals, 9)`, so `--decimals 18` on Solana produced a 9-decimal
    # mint and said nothing; and `--uri` was accepted on an EVM deploy and
    # dropped on the floor, so a token deployed with a logo had none. A flag
    # that cannot be honoured is REFUSED, and a default that differs by chain
    # is resolved from the chain, not from a literal.
    if decimals is None:
        decimals = 9 if is_solana else 18
    elif is_solana and decimals > 9:
        raise click.ClickException(
            f"--decimals {decimals} is above Solana's ceiling of 9 (supply x "
            f"10**decimals must fit in a u64). Pass --decimals 9 or lower; "
            f"this used to be clamped silently, which produced a token with "
            f"different decimals from the one you asked for.")
    if uri and not is_solana:
        raise click.ClickException(
            "--uri is SOLANA only: an EVM ERC-20 carries no metadata URI, so "
            "there is nowhere for this to go. It used to be accepted and "
            "dropped — a launch with no logo that reported success.")

    if is_solana:
        from tools.defi.spl_deploy_verb import perform_solana_deploy_token
        from tools.defi.trade_tool import SolanaDeployTokenParams

        if vanity or salt:
            raise click.ClickException(
                "--vanity/--salt are the EVM CREATE2 factory. A Solana mint "
                "address is a keypair, not a hash of its code — there is "
                "nothing to mine against here.")
        if execute and not yes:
            click.echo(click.style(
                f"About to mint {supply:g} {symbol} on Solana and revoke the "
                f"mint authority. This cannot be undone.", fg="yellow"))
            click.confirm("Proceed?", abort=True)
        sol_params = SolanaDeployTokenParams(
            name=label, symbol=symbol, supply=supply, uri=uri,
            decimals=int(decimals), max_spend_usd=max_usd,
            dry_run=not execute)
        result = asyncio.run(perform_solana_deploy_token(
            DefiTradeTool(), sol_params, _owner_ctx()))
        _echo_result(result, what="deployment")
        return
    if execute and not yes:
        click.echo(click.style(
            f"About to deploy {symbol} ({label}) with a fixed supply of "
            f"{supply:g} on {chain}. A deployment cannot be undone and the "
            f"supply can never change.", fg="yellow"))
        click.confirm("Proceed?", abort=True)

    params = DeployTokenParams(chain=chain, name=label, symbol=symbol,
                               supply=supply, decimals=decimals,
                               max_spend_usd=max_usd, vanity=vanity, salt=salt,
                               dry_run=not execute)
    result = asyncio.run(perform_deploy_token(DefiTradeTool(), params,
                                              _owner_ctx()))
    _echo_result(result, what="deployment")


@wallet_cmd.command("launch")
@click.argument("symbol")
@click.argument("name", nargs=-1, required=True)
@click.option("--buy", type=float, default=0.0, show_default=True,
              help="Opening buy in the chain's NATIVE asset. 0 launches with no "
                   "opening position, which also means anyone else takes the "
                   "first one.")
@click.option("--creator-tax-bps", type=int, default=100, show_default=True,
              help="Creator tax on every curve trade, paid to this wallet.")
@click.option("--logo", default="",
              help="Logo URI (https:// or ipfs://). A launch with no logo looks "
                   "abandoned next to the ones that have one.")
@click.option("--desc", default="", help="Short description shown on the launchpad.")
@click.option("--x", "twitter", default="", help="X/Twitter URL.")
@click.option("--site", default="", help="Website URL.")
@click.option("--max-usd", type=float, default=0.0,
              help="The most this launch may cost. Default: sized from --buy.")
@click.option("--execute", is_flag=True, default=False,
              help="Actually broadcast. Without it this reads the live terms, "
                   "prices the opening buy and reports.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the typed confirmation.")
@as_root_option
def wallet_launch(symbol, name, buy, creator_tax_bps, logo, desc, twitter,
                  site, max_usd, execute, yes):
    """Launch a token on the Pons launchpad: polyrob wallet launch ROB Rob Coin

    Deploys the token AND its bonding curve in one transaction on Robinhood
    Chain. Fixed 1B supply into a curve that graduates to a locked Uniswap pool.

    Every term (fee, supply, graduation threshold) is read LIVE and committed, so
    terms that move between the quote and the broadcast revert rather than
    silently applying.

    WARNING: everything on a launchpad is a memecoin. The caps bound what this
    can spend; they do not make it a good idea.
    """
    import asyncio

    from tools.launchpad.tool import LaunchParams, LaunchpadTool

    label = " ".join(name)
    ceiling = max_usd if max_usd > 0 else max(25.0, (buy + 0.01) * 6000.0)
    if execute and not yes:
        click.echo(click.style(
            f"About to launch {symbol} ({label}) on Pons"
            + (f" with an opening buy of {buy:g} native." if buy else " with no "
               "opening buy.")
            + " This cannot be undone.", fg="yellow"))
        click.confirm("Proceed?", abort=True)

    if not logo:
        click.echo(click.style(
            "note: no --logo. On a launchpad that is how a token looks "
            "abandoned.", fg="yellow"))
    params = LaunchParams(name=label, symbol=symbol, buy_amount=buy,
                          creator_tax_bps=creator_tax_bps, logo=logo,
                          description=desc, twitter=twitter, website=site,
                          max_spend_usd=ceiling, dry_run=not execute)
    result = asyncio.run(LaunchpadTool().launchpad_launch(params, _owner_ctx()))
    _echo_result(result, what="launch")


@wallet_cmd.command("curve")
@click.argument("token")
@click.option("--buy", type=float, default=0.0,
              help="Price buying this much of the quote asset.")
@click.option("--sell", type=float, default=0.0,
              help="Price selling this many tokens.")
def wallet_curve(token, buy, sell):
    """Read a launchpad token's curve, or price a trade on it.

    polyrob wallet curve 0xabc…            — state and graduation progress
    polyrob wallet curve 0xabc… --buy 0.1  — what 0.1 native would buy
    """
    import asyncio

    from tools.launchpad.tool import LaunchpadTool, QuoteParams, StatusParams

    tool = LaunchpadTool()
    # C36: `side="buy" if buy > 0 else "sell"` meant `--buy 1 --sell 500` priced
    # a BUY and said nothing about the sell — the quote answered a question the
    # operator did not ask, in the one place a wrong side is a wrong trade.
    if buy > 0 and sell > 0:
        raise click.ClickException(
            "pass --buy OR --sell, not both: a curve quote has ONE side, and "
            "the sell used to be silently ignored.")
    if buy > 0 or sell > 0:
        params = QuoteParams(token=token, side="buy" if buy > 0 else "sell",
                             amount=buy if buy > 0 else sell)
        result = asyncio.run(tool.launchpad_quote(params, _owner_ctx()))
    else:
        result = asyncio.run(
            tool.launchpad_status(StatusParams(token=token), _owner_ctx()))
    _echo_result(result, what="curve read")


# ---------------------------------------------------------------------------
# E6 / E7 / E9 (2026-09-21): three capability planes that existed only as agent
# actions. Each is a THIN consumer of the SAME tool method the agent calls —
# never a second implementation, so no gate, refusal or proof can differ
# between the terminal and the agent.
#
# ⚠️ No new TOP-LEVEL group: 043 cut the CLI to 30 names and pinned it there.
# ---------------------------------------------------------------------------

@wallet_cmd.command("claim")
@click.argument("token")
@click.option("--max-usd", type=float, default=1.0, show_default=True,
              help="The most this may cost. A claim RECEIVES; it sends "
                   "nothing, so this bounds the gas fee.")
@click.option("--execute", is_flag=True, default=False,
              help="Actually broadcast. Without it this reads the escrow and "
                   "reports what would arrive.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the typed confirmation.")
@as_root_option
def wallet_claim(token, max_usd, execute, yes):
    """Claim the creator fees a token you launched has earned.

    E6: `launchpad_status` told its reader to "run launchpad_claim", which is
    an AGENT action name — there was no owner seat for it at all, and 13.6 ETH
    of creator tax sat in an escrow nobody could reach from a terminal.

    ⚠️ The escrow credits an ADDRESS, not a token: ONE claim collects what
    every token this wallet launched has earned. Naming a token here only tells
    the tool which curve to read the escrow address from — there is no
    parameter through which the claim can be aimed elsewhere.
    """
    import asyncio

    from tools.launchpad.tool import ClaimParams, LaunchpadTool

    if execute and not yes:
        click.echo(click.style(
            f"About to claim the creator fees owed to this wallet (read from "
            f"{token}'s curve escrow). This broadcasts a transaction.",
            fg="yellow"))
        click.confirm("Proceed?", abort=True)
    params = ClaimParams(token=token, max_spend_usd=max_usd, dry_run=not execute)
    result = asyncio.run(LaunchpadTool().launchpad_claim(params, _owner_ctx()))
    _echo_result(result, what="claim")


@wallet_cmd.group("nft")
def wallet_nft():
    """Non-fungibles this wallet holds: look, send, revoke an approval.

    ⚠️ Enumeration needs an indexer (ALCHEMY_API_KEY). Without one the read
    SAYS it could not look — an empty list would read as "you own nothing",
    which is a different fact.
    """


@wallet_nft.command("list")
@click.option("--chain", default="base", show_default=True)
@click.option("--address", default=None,
              help="Address to inspect (default: this agent's own wallet).")
def wallet_nft_list(chain, address):
    """What this wallet holds on CHAIN."""
    import asyncio

    from tools.defi.data_tool import DefiDataTool, NftHoldingsParams

    result = asyncio.run(DefiDataTool().nft_holdings(
        NftHoldingsParams(chain=chain, address=address), _owner_ctx()))
    _echo_result(result, what="holdings read")


@wallet_nft.command("info")
@click.argument("contract")
@click.argument("token_id", type=int)
@click.option("--chain", default="base", show_default=True)
def wallet_nft_info(contract, token_id, chain):
    """Owner, metadata and approvals for ONE token."""
    import asyncio

    from tools.defi.data_tool import DefiDataTool, NftInfoParams

    result = asyncio.run(DefiDataTool().nft_info(
        NftInfoParams(chain=chain, contract=contract, token_id=token_id),
        _owner_ctx()))
    _echo_result(result, what="token read")


@wallet_nft.command("transfer")
@click.argument("contract")
@click.argument("token_id", type=int)
@click.argument("to")
@click.option("--chain", default="base", show_default=True)
@click.option("--standard", type=click.Choice(["erc721", "erc1155"]),
              default="erc721", show_default=True,
              help="Say which — guessing encodes a call the contract may misread.")
@click.option("--amount", type=int, default=1, show_default=True,
              help="Quantity, erc1155 only (an erc721 is unique).")
@click.option("--max-usd", type=float, default=25.0, show_default=True,
              help="Ceiling for the transaction FEE. ⚠️ It does NOT bound what "
                   "you are sending — an NFT has no reliable price.")
@click.option("--execute", is_flag=True, default=False,
              help="Actually broadcast. Without it this simulates and reports.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the typed confirmation.")
@as_root_option
def wallet_nft_transfer(contract, token_id, to, chain, standard, amount,
                        max_usd, execute, yes):
    """Send a token. IRREVERSIBLE, and ALWAYS owner-approved.

    ⚠️ Every gate the agent meets applies here unchanged — this verb is in
    `spend_lane.ALWAYS_OWNER_APPROVED_VERBS`, so no autonomous-spend ceiling
    can ever exempt it. The CLI is the owner seat; it does not bypass a gate,
    it satisfies one.
    """
    import asyncio

    from tools.defi.trade_tool import DefiTradeTool, NftTransferParams

    if execute and not yes:
        click.echo(click.style(
            f"About to send {contract} #{token_id} to {to} on {chain}. A "
            f"transfer cannot be undone and an NFT has no price this can cap.",
            fg="yellow"))
        click.confirm("Proceed?", abort=True)
    result = asyncio.run(DefiTradeTool().nft_transfer(
        NftTransferParams(chain=chain, contract=contract, token_id=token_id,
                          to=to, standard=standard, amount=amount,
                          max_spend_usd=max_usd, dry_run=not execute),
        _owner_ctx()))
    _echo_result(result, what="transfer")


@wallet_nft.command("revoke")
@click.argument("contract")
@click.argument("operator")
@click.option("--chain", default="base", show_default=True)
@click.option("--max-usd", type=float, default=5.0, show_default=True)
@click.option("--execute", is_flag=True, default=False,
              help="Actually broadcast. Without it this simulates and reports.")
@as_root_option
def wallet_nft_revoke(contract, operator, chain, max_usd, execute):
    """Retire an operator's blanket approval over a collection (risk-reducing)."""
    import asyncio

    from tools.defi.trade_tool import DefiTradeTool, NftRevokeParams

    result = asyncio.run(DefiTradeTool().nft_revoke_approval(
        NftRevokeParams(chain=chain, contract=contract, operator=operator,
                        max_spend_usd=max_usd, dry_run=not execute),
        _owner_ctx()))
    _echo_result(result, what="revoke")


@wallet_cmd.group("dapp")
def wallet_dapp():
    """Web-dapp wallet sessions: what is connected, and how to end one."""


def _dapp_store(*, write: "bool | None" = None):
    """The durable dapp-session store under the ADMIN home, or ``None``.

    A read never CREATES the store: an absent file means no session was ever
    connected on this box, and saying so beats materialising a db to answer
    "none".

    *write* is the 057 WS-G euid declaration and is passed straight to the
    admin-home seam: ``revoke`` MUTATES the shared home, so it must refuse a
    root run rather than only warn (a root-owned row is what the service later
    meets as "attempt to write a readonly database").
    """
    import os as _os

    from core.dapp_session_store import DappSessionStore
    from core.runtime_paths import data_home_db_path
    db = data_home_db_path("dapp_sessions.db", data_dir=_admin_home(write=write))
    if not _os.path.exists(db):
        return None
    return DappSessionStore(db)


@wallet_dapp.command("list")
@click.option("--user", "user_id", default=None, help="Tenant id (default: this identity).")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def wallet_dapp_list(user_id, as_json):
    """Every dapp wallet session this tenant has connected, newest first."""
    tenant = _admin_tenant(user_id)
    store = _dapp_store(write=False)
    rows = [] if store is None else store.list_for_tenant(tenant)
    if as_json:
        click.echo(_json.dumps(
            [{"session_id": r.session_id, "revoked": r.revoked,
              "created_at": r.created_at, "updated_at": r.updated_at,
              "envelope": r.envelope} for r in rows], indent=2, default=str))
        return
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("dapp wallet sessions",
                         f"tenant {tenant} has never connected one"))
        return
    for r in rows:
        env = r.envelope or {}
        spent = float(env.get("spent_usd") or 0.0)
        budget = float(env.get("session_budget_usd") or 0.0)
        state = "REVOKED" if (r.revoked or env.get("revoked")) else "live record"
        click.echo(f"  {r.session_id}  [{state}]  {env.get('chain') or '?'}  "
                   f"{env.get('address') or 'address unknown'}")
        click.echo(f"     spent ${spent:.4f} of ${budget:.2f} · "
                   f"signed {len(env.get('sent') or [])} · "
                   f"refused {len(env.get('refused') or [])}")


@wallet_dapp.command("revoke")
@click.argument("session_id")
@click.option("--user", "user_id", default=None, help="Tenant id (default: this identity).")
@as_root_option
def wallet_dapp_revoke(session_id, user_id):
    """Revoke a dapp wallet session's durable record.

    ⚠️ This flips the RECORD. A bridge still live inside a running agent
    process holds its own in-memory envelope, and the agent's own
    `dapp_disconnect` is what ends that one immediately — say so rather than
    implying the page is cut off this instant.
    """
    tenant = _admin_tenant(user_id)
    store = _dapp_store(write=True)
    if store is None:
        raise click.ClickException(
            "there is no dapp-session store on this box, so there is nothing "
            "to revoke. That is 'never connected', not 'revoked'.")
    row = store.get(session_id, user_id=tenant)
    if row is None:
        raise click.ClickException(
            f"no dapp session {session_id!r} for tenant {tenant} "
            f"(`polyrob wallet dapp list` shows the ids).")
    if row.revoked:
        click.echo(click.style(f"{session_id} was already revoked.", fg="yellow"))
        return
    store.mark_revoked(session_id)
    click.echo(click.style(f"revoked the dapp session record {session_id}.",
                           fg="green"))
    click.echo(click.style(
        "  a bridge still live in a running agent process keeps its in-memory "
        "envelope until that session ends — ask the agent to run "
        "`dapp_disconnect` to cut it off now.", dim=True))


# ---------------------------------------------------------------------------
# 046: the assets the treasury may be paid in.
#
# ⚠️ No new TOP-LEVEL CLI group. 043 cut the top level from 45 names to 30 and
# pinned it there; `wallet` already owns chain and token identity, so the asset
# verbs belong here.
#
# ⚠️ These are the ONLY writers of an operator asset row. There is deliberately
# no agent action and no chat verb that creates one — an asset is an operator
# grant, the line proposal 036 drew for standing work.
# ---------------------------------------------------------------------------

@wallet_cmd.group("asset")
def wallet_asset():
    """Assets the treasury may be paid in (proposal 046). Operator-only."""


def _asset_rpc(ctx: click.Context, chain: str):
    """The RPC callable for *chain* — an injected one (tests) or the pinned one."""
    injected = (ctx.obj or {}).get("rpc_call") if isinstance(ctx.obj, dict) else None
    if injected is not None:
        return injected
    from core.wallet.onchain import _rpc, rpc_url_for_chain
    url = rpc_url_for_chain(chain)
    if not url:
        raise click.ClickException(
            f"no RPC pinned for chain {chain!r} — set DEFI_EVM_RPC_"
            f"{chain.upper().replace('-', '_')} first")
    return lambda method, params: _rpc(url, method, params)


def _require_known_chain(chain: str) -> str:
    from core.wallet import chains
    chain = (chain or "").strip().lower()
    if chains.get(chain) is None:
        raise click.ClickException(
            f"unknown chain {chain!r} — core/wallet/chains.py has rows for "
            f"{', '.join(chains.names())}")
    return chain


def _require_address(chain: str, address: str) -> str:
    """The canonical form of *address* on *chain* — the ONE normalizer.

    C13: this lower-cased the argument and checked only "0x + 40 hex", so a
    mis-typed mixed-case address whose EIP-55 checksum does not verify was
    accepted and FROZEN into an asset row that denominates money; and a Solana
    address, having already passed the chain check one line above, was rejected
    here as "not an EVM token address" — which reads as "your address is wrong"
    for an address that is right. ``core.wallet.addresses.normalize_for_chain``
    owns both rules, per family, and refuses rather than assuming a family.
    """
    from core.wallet.addresses import normalize_for_chain
    try:
        return normalize_for_chain(chain, (address or "").strip())
    except ValueError as exc:
        raise click.ClickException(str(exc))


@wallet_asset.command("add")
@click.option("--id", "asset_id", required=True, help="short id, e.g. rob")
@click.option("--chain", required=True)
@click.option("--address", required=True)
@click.option("--decimals", type=int, default=None,
              help="required unless --verify reads them from the chain")
@click.option("--symbol", default="")
@click.option("--min-amount", "min_amount", default="0",
              help="hard floor in RAW token units for any invoice in this asset")
@click.option("--liquidity-floor", type=float, default=0.0,
              help="refuse to price against a pool shallower than this (USD)")
@click.option("--verify", is_flag=True,
              help="read decimals()/symbol() on-chain ONCE and freeze them")
@click.pass_context
@as_root_option
def wallet_asset_add(ctx, asset_id, chain, address, decimals, symbol,
                     min_amount, liquidity_floor, verify):
    """Pin an asset the treasury may be paid in.

    ⚠️ ``decimals`` denominates every amount comparison the settlement scan
    makes, so it is FROZEN here and never re-read. Prefer --verify.
    """
    import time

    from core.payments.assets import (AssetStore, PaymentAsset, store_path,
                                      verify_on_chain)

    chain = _require_known_chain(chain)
    address = _require_address(chain, address)

    if verify:
        decimals, symbol = verify_on_chain(chain, address,
                                           rpc_call=_asset_rpc(ctx, chain))
        click.echo(f"verified on {chain}: decimals={decimals} symbol={symbol}")
    if decimals is None:
        raise click.ClickException(
            "--decimals is required without --verify (never guess decimals: "
            "they denominate money)")
    try:
        floor = int(min_amount)
    except ValueError:
        raise click.ClickException(f"--min-amount {min_amount!r} is not an integer "
                                   f"(it is RAW token units, not a decimal amount)")

    AssetStore(store_path()).upsert(PaymentAsset(
        asset_id=asset_id.strip().lower(), chain=chain, address=address,
        decimals=int(decimals), symbol=symbol or "", rail="onchain_scan",
        min_amount_raw=floor, liquidity_floor_usd=float(liquidity_floor),
        verified_at=time.time(), source="operator"))
    click.echo(f"✅ {asset_id} = {symbol or '?'} on {chain} "
               f"({decimals} decimals), floor {floor} raw units.")


@wallet_asset.command("list")
def wallet_asset_list():
    """Every asset the treasury can be paid in, and where each row came from."""
    from core.payments.assets import all_assets

    for a in all_assets():
        click.echo(f"{a.asset_id:24} {a.symbol:8} {a.chain:14} "
                   f"{a.decimals:>2}d  {a.rail:14} {a.source}")


@wallet_asset.command("verify")
@click.argument("asset_id")
@click.pass_context
def wallet_asset_verify(ctx, asset_id):
    """Re-read the contract and REPORT. Never rewrites a frozen row."""
    from core.payments.assets import resolve, verify_on_chain

    row = resolve(asset_id)
    if row is None:
        raise click.ClickException(f"unknown asset {asset_id!r}")
    if not row.address:
        raise click.ClickException(f"{asset_id} is a native coin — nothing to read")
    decimals, symbol = verify_on_chain(row.chain, row.address,
                                       rpc_call=_asset_rpc(ctx, row.chain))
    if decimals != row.decimals or symbol != row.symbol:
        click.echo(
            f"⚠️ metadata_changed: {asset_id} is pinned at decimals="
            f"{row.decimals} symbol={row.symbol!r} and now reports decimals="
            f"{decimals} symbol={symbol!r}. The pin is UNCHANGED — money math "
            f"reads the frozen value. Investigate before you trust this token.")
        return
    click.echo(f"✅ {asset_id} still reports decimals={decimals} symbol={symbol}.")


from cli.commands.wallet_lp import lp_cmd
wallet_cmd.add_command(lp_cmd)


@wallet_cmd.command("overview")
@click.option("--user", "user_id", default=None, help="Tenant id (default: this identity).")
@click.option("--json", "as_json", is_flag=True)
def wallet_overview(user_id, as_json):
    """Shared wallet identities, cached balances, limits and unresolved sends."""
    from dataclasses import asdict

    from core.wallet.view import render_wallet, wallet_view
    # C29: the view resolved its own home, so on a deployed box it read a tree
    # the service never writes — and a PermissionError on that tree escaped as
    # a raw traceback rather than a sentence naming the cause and the remedy.
    _admin_home(write=False)
    try:
        view = wallet_view(_admin_tenant(user_id))
    except PermissionError as exc:
        raise click.ClickException(
            f"the wallet view could not be read ({exc}). That is UNKNOWN, not "
            f"an empty wallet — on a deployed box run it as the service "
            f"identity: sudo -u polyrob-agent polyrob wallet overview")
    except Exception as exc:
        raise click.ClickException(
            f"the wallet view could not be built ({type(exc).__name__}: {exc}). "
            f"That is UNKNOWN, not an empty wallet.")
    # C74: `__import__("json")` — the module is already imported at the top as
    # `_json`; the dynamic form hides the dependency from every reader and tool.
    click.echo(_json.dumps(asdict(view), default=str) if as_json
               else render_wallet(view))
