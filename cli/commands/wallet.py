"""`polyrob wallet` — show the agent wallet: per-venue addresses, on-chain
balances, network, caps, and which venue is OPERATIONAL (the one funded + spent from).

This closes the interface gap that produced the 2026-07-08 fund-the-wrong-address
incident: the owner had no single place to see "what's my address / balance / which
one do I fund". Balances are best-effort over public RPCs (fail-open to n/a).

`polyrob wallet set-cap <daily|per-tx> <usd>` (owner-UX P2 T7) is the guided,
confirmed way to raise/lower the two money-authoritative env caps
(`WALLET_DAILY_CAP_USD` / `AGENT_WALLET_MAX_PER_TX_USD`) — these stay
env-authoritative; a per-user preference (`core.prefs`, ``budget.wallet_*``)
may only tighten below the env value, never raise it.
"""
from __future__ import annotations

import json as _json
import math
import os
from pathlib import Path

import click

from cli.commands.config import _upsert_env
from core.paths import polyrob_home
from core.instance import resolve_owner_user_id
from core.wallet.onchain import VENUE_CHAIN as _VENUE_CHAIN, balances as _balances

# Only these venues hold a same-chain float the agent spends directly. hyperliquid
# (delegated signer, collateral in the master account) and polymarket (per-user proxy
# creds) NEVER hold funds at their derived address — showing a fundable balance there
# would re-create the fund-the-wrong-address footgun this whole change exists to kill.
_FUNDABLE = {"treasury", "x402"}

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
        try:
            solana_address = w.solana_address
        except Exception:
            solana_address = None
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
               "venues": venues, "caps": caps}

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
    if solana_address:
        sol_line = f"  {'solana':11s} {solana_address}  [solana] ←FUND for Solana trades"
        if not no_balances and cfg.network == "mainnet":
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
            except Exception:
                pass
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
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the global env-file home (test/ops only).")
def set_cap_cmd(kind: str, usd: str, yes: bool, home_dir_opt: str | None):
    """Set the wallet's DAILY or PER-TX USD spend cap.

    Guided, confirmed write of the money-authoritative env var — writes
    WALLET_DAILY_CAP_USD (daily) or AGENT_WALLET_MAX_PER_TX_USD (per-tx) to
    the GLOBAL env file (~/.polyrob/.env). Money stays env-authoritative: a
    per-user preference may only tighten below this value, never raise it.
    `set-cap daily none` (or off/unlimited/disabled) explicitly disables the
    aggregate cap — the ONLY way to do so (H3, 2026-08-22): an absent env var
    now means the finite $100/24h default, not "no cap".
    """
    value_to_write = _parse_cap_arg(kind, usd)
    key = _CAP_ENV_KEY[kind]
    home = Path(home_dir_opt) if home_dir_opt else polyrob_home()
    path = home / ".env"
    line = f"{key}={value_to_write}"

    click.echo(f"About to write to {path}:")
    click.echo(f"  {line}")
    if not yes and not click.confirm("Proceed?", default=False):
        click.echo("Aborted — no changes written.")
        return

    _upsert_env(path, key, value_to_write, secure=True)

    from core.wallet.config import _CAP_DISABLED
    if kind == "daily" and value_to_write in _CAP_DISABLED:
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
    data_dir = _P(data_dir_opt) if data_dir_opt else None
    run_wallet_init_flow(mnemonic=mnemonic or None, raw_seed=raw_seed or None, home=home,
                         assume_yes=yes, data_dir=data_dir)


# ---------------------------------------------------------------------------
# 037 — cross-chain bridge (owner seat)
# ---------------------------------------------------------------------------

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

    from core.instance import resolve_owner_user_id
    from core.runtime_paths import resolve_data_home
    from core.surfaces.inbox_render import render_book
    from tools.defi.book import read_book

    uid = user_id or resolve_owner_user_id()
    try:
        body = asyncio.run(read_book(uid, str(resolve_data_home())))
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
    from core.instance import resolve_owner_user_id
    from core.wallet import bridge_guard

    uid = user_id or resolve_owner_user_id()
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
    for r in rows:
        import datetime as _dt
        when = _dt.datetime.utcfromtimestamp(float(r["created_at"])).strftime("%Y-%m-%d %H:%MZ")
        usd = "" if r.get("amount_usd") is None else f" ${float(r['amount_usd']):,.2f}"
        dest = _chain_name(r.get("dest_chain_id"))
        click.echo(f"  {r['id']}  [{r['state']}]{usd}  -> {dest}  {when}")
        click.echo(f"     relay request: {r['request_id']}")
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
                   "arbitrary address is refused.")
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
    from types import SimpleNamespace

    from tools.defi.bridge_verb import perform_bridge
    from tools.defi.trade_tool import BridgeParams, DefiTradeTool

    params = BridgeParams(from_chain=from_chain, to_chain=to_chain, amount=amount,
                          token_out=token_out, dry_run=not execute)
    tool = DefiTradeTool()

    if execute and not yes:
        click.echo(click.style(
            f"About to bridge {amount} native {from_chain} -> {to_chain}. "
            f"This moves real funds and cannot be undone.", fg="yellow"))
        click.confirm("Proceed?", abort=True)

    # The CLI IS the owner seat, so there is no forged-turn context to pass.
    ctx = SimpleNamespace(user_id=resolve_owner_user_id(), role="owner", is_sub_agent=False)
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
    """The CLI IS the owner seat, so there is no forged-turn context to pass."""
    from types import SimpleNamespace
    return SimpleNamespace(user_id=resolve_owner_user_id(), role="owner", is_sub_agent=False,
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
@click.option("--decimals", type=int, default=18, show_default=True)
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
                   "comes from; without it the token shows with no picture.")
@click.option("--execute", is_flag=True, default=False,
              help="Actually broadcast. Without it this simulates, asserts the "
                   "produced bytecode against the pinned template, and reports "
                   "the address it WOULD land at.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the typed confirmation.")
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

    if str(chain).strip().lower() == "solana":
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
            decimals=min(int(decimals), 9), max_spend_usd=max_usd,
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
    if buy > 0 or sell > 0:
        params = QuoteParams(token=token, side="buy" if buy > 0 else "sell",
                             amount=buy if buy > 0 else sell)
        result = asyncio.run(tool.launchpad_quote(params, _owner_ctx()))
    else:
        result = asyncio.run(
            tool.launchpad_status(StatusParams(token=token), _owner_ctx()))
    _echo_result(result, what="curve read")


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


def _require_evm_address(address: str) -> str:
    address = (address or "").strip().lower()
    ok = address.startswith("0x") and len(address) == 42
    if ok:
        try:
            int(address[2:], 16)
        except ValueError:
            ok = False
    if not ok:
        raise click.ClickException(f"{address!r} is not an EVM token address")
    return address


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
    address = _require_evm_address(address)

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
@click.option("--json", "as_json", is_flag=True)
def wallet_overview(as_json):
    """Shared wallet identities, cached balances, limits and unresolved sends."""
    from dataclasses import asdict
    from core.wallet.view import wallet_view, render_wallet
    view = wallet_view(resolve_owner_user_id())
    click.echo(__import__("json").dumps(asdict(view)) if as_json else render_wallet(view))
