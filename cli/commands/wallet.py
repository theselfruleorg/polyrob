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
# pref/env merge helpers) are now real callers wired into load_wallet_config()
# -> PolicyGate (owner-UX G-13): a per-tenant preference (budget.wallet_daily_usd
# / budget.wallet_per_tx_usd) can TIGHTEN this env cap further, min-merged, but
# can never raise or disable it — the env value set here always remains the
# ceiling. Verify wiring: `grep -rn "effective_daily_cap_usd\|effective_max_per_tx_usd"
# core/ modules/ tools/ | grep -v test`.
_POLICY_GATE_CAVEAT = (
    "note: a per-tenant preference can TIGHTEN this cap further (min-merged); "
    "it can never raise or disable it — this env value stays the ceiling."
)

_ENABLED_TRUE = {"1", "true", "yes", "on"}


def _is_malformed_number(raw) -> bool:
    """True iff ``raw`` is a non-empty string that does NOT parse as a float.

    Used to NAME the offending cap env var (M12) when load_wallet_config's bare
    ``float()`` raises. An unset/empty value is NOT malformed (it just falls back
    to the default), so only a present-but-garbage value trips this.
    """
    text = (raw or "").strip()
    if not text:
        return False
    try:
        float(text)
        return False
    except (TypeError, ValueError):
        return True


def _parse_positive_usd(raw: str) -> float:
    """Validate a cap amount: a positive, finite number.

    A cap of 0 is deliberately NOT accepted as "disabled" — that ambiguity
    (0 == disabled vs. 0 == "spend nothing") is exactly the kind of footgun
    this guided command exists to avoid. Disabling a cap is an explicit,
    separate action: remove the env var.
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
            "(to disable a cap, remove the env var instead)"
        )
    return value


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
        enabled = str(os.environ.get("AGENT_WALLET_ENABLED", "")).strip().lower() in _ENABLED_TRUE
        bad_caps = [k for k in ("AGENT_WALLET_MAX_PER_TX_USD", "WALLET_DAILY_CAP_USD")
                    if _is_malformed_number(os.environ.get(k))]
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
               "address": wallet_address, "venues": venues, "caps": caps}

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
@click.option("--yes", is_flag=True, default=False,
              help="Skip the confirmation prompt (non-interactive use).")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the global env-file home (test/ops only).")
def set_cap_cmd(kind: str, usd: str, yes: bool, home_dir_opt: str | None):
    """Set the wallet's DAILY or PER-TX USD spend cap.

    Guided, confirmed write of the money-authoritative env var — writes
    WALLET_DAILY_CAP_USD (daily) or AGENT_WALLET_MAX_PER_TX_USD (per-tx) to
    the GLOBAL env file (~/.polyrob/.env). Money stays env-authoritative: a
    per-user preference may only tighten below this value, never raise it.
    """
    value = _parse_positive_usd(usd)
    key = _CAP_ENV_KEY[kind]
    home = Path(home_dir_opt) if home_dir_opt else polyrob_home()
    path = home / ".env"
    line = f"{key}={usd.strip()}"

    click.echo(f"About to write to {path}:")
    click.echo(f"  {line}")
    if not yes and not click.confirm("Proceed?", default=False):
        click.echo("Aborted — no changes written.")
        return

    _upsert_env(path, key, usd.strip(), secure=True)

    label = "daily cap" if kind == "daily" else "per-transaction cap"
    click.echo(f"Wrote {key}={value} to {path} ({label}).")
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

    # M13 (2026-07-15): surface the REAL spend posture at init — the default per-tx
    # is a catastrophic-loss ceiling, and with no daily cap the agent can make
    # unlimited sub-ceiling txs. A new owner never saw this before.
    max_tx = (os.environ.get("AGENT_WALLET_MAX_PER_TX_USD") or "1000").strip()
    daily = (os.environ.get("WALLET_DAILY_CAP_USD") or "").strip()
    if daily:
        click.echo(f"Spend caps: ${max_tx}/tx ceiling · ${daily}/day budget.")
    else:
        click.echo(f"Spend caps: ${max_tx}/tx (a catastrophic-loss CEILING, not a budget) "
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
        raise click.ClickException("no wallet configured — run `polyrob wallet init` first")

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
    click.echo("\n⚠ Clear your terminal scrollback/shell history after copying "
               "(this output is exactly as sensitive as the funds).")


@wallet_cmd.command("init")
@click.option("--from-mnemonic", "mnemonic", default=None,
              help="Import an existing BIP-39 mnemonic instead of generating one.")
@click.option("--from-seed", "raw_seed", default=None,
              help="Import a legacy raw seed (>=32 chars) — keeps a pre-BIP44 install's addresses.")
@click.option("--yes", is_flag=True, default=False, help="No prompts (accept defaults).")
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
