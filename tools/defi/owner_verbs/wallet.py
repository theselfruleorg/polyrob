"""``/wallet`` — addresses, network, caps, and the one cap the owner sets from chat.

The body every seat runs (proposal 125 S14): the Telegram seat table
(``surfaces/telegram/admin_verbs.py``), the REPL and the console import it from here.
It reads ``core.wallet`` and writes only ``budget.defi_autonomous_usd`` — a
setting that cannot widen maximum loss (``tx_guard.autonomous_max_usd`` clamps it
to the per-transaction ceiling). ``/wallet signed [n]`` reads the one signing
journal (113 W6 G-5): it signs nothing and changes no rule; an unreadable journal
is not an empty one.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, List, Optional

from core.wallet.onchain import FUNDABLE_VENUES

logger = logging.getLogger(__name__)


def _code(text: Any) -> str:
    return f"`{text}`"


#: ⚠️ This used to send him to SSH: "caps are env-authoritative … plus a
#: restart". That stopped being true on 2026-09-18, when the per-transaction
#: ceiling became an owner-approved preference that RAISES the env value,
#: clamped by the daily cap. He approved a raise twice while this note told him
#: it could not work, and the agent then asked him to edit an env file.
CAP_NOTE = (
    "Change a cap (it asks for your approval, then applies live): "
    "/config set budget.wallet_per_tx_usd <usd> · "
    "/config set budget.wallet_daily_usd <usd> (it can only lower the operator's cap). "
    "What runs without asking you: /wallet autonomous <usd>."
)


def set_autonomous_ceiling(rest: List[str], user_id: Optional[str],
                            data_dir: Optional[str]) -> str:
    """Write ``budget.defi_autonomous_usd`` — how much runs without asking.

    The owner had to SSH in and edit an env file to change this, which is why
    a $96 bridge sat behind a $5 ceiling for a day. It is safe from chat
    precisely because it is not a loss limit: ``tx_guard.autonomous_max_usd``
    clamps it to the catastrophic per-transaction ceiling, so raising it can
    only reduce how often the owner is interrupted.
    """
    if not user_id:
        return "Only the owner can change spend settings."
    if not rest:
        return ("Usage: /wallet autonomous <usd>\n"
                "How much may execute without asking you. Anything above it "
                "queues for /approve. It can never exceed the per-transaction "
                "ceiling; change that one with /config set "
                "budget.wallet_per_tx_usd <usd> (owner-approved, clamped to "
                "the daily cap).")
    try:
        value = float(str(rest[0]).lstrip("$"))
    except (TypeError, ValueError):
        return f"{_code(rest[0])} is not a number — usage: /wallet autonomous <usd>"
    if value < 0:
        return "A ceiling cannot be negative."
    try:
        from core import prefs
        from core.runtime_paths import prefs_home_dir
        written, why = prefs.write_preference(prefs_home_dir(), user_id,
                                              "budget.defi_autonomous_usd", value)
        if not written:
            return f"Could not save it: {why}"
        from core.wallet import tx_guard
        effective = tx_guard.autonomous_max_usd(user_id, prefs_home_dir())
    except Exception as e:
        logger.warning("autonomous ceiling not written: %s", e)
        return f"Could not save it: {e}"
    out = [f"✅ Autonomous ceiling set to ${value:,.2f}.",
           f"Effective now: ${effective:,.2f} — spends up to this run without "
           f"asking you; above it they queue for /approve."]
    if effective < value:
        out.append(f"⚠️ Clamped to the per-transaction ceiling ${effective:,.2f}. "
                   f"That one is a loss limit: raise it with /config set "
                   f"budget.wallet_per_tx_usd <usd> (guarded → /approve), and it "
                   f"stays clamped to the daily cap.")
    return "\n".join(out)


def wallet_reply(args: List[str], user_id: Optional[str] = None,
                 data_dir: Optional[str] = None) -> str:
    """`/wallet` — addresses, network, caps, and the one cap the owner may set.

    On-chain balance probes are network reads, so they are opt-in via
    `/wallet balances` rather than paid for on every status glance.

    `/wallet autonomous <usd>` sets how much executes WITHOUT interrupting the
    owner, with no queue: it cannot widen maximum loss, because the per-transaction
    ceiling still binds above it (and is clamped to it at the read site). That ceiling
    moves only through the guarded `budget.wallet_per_tx_usd` pref, which queues on chat.
    """
    from core.wallet.authority import owner_refusal
    refusal = owner_refusal(user_id)
    if refusal:
        return refusal
    if args and args[0].lower() in ("autonomous", "auto", "ceiling"):
        return set_autonomous_ceiling(args[1:], user_id, data_dir)
    if args and args[0].lower() == "income":
        # 122 W4: the owner's income allocation rule — written only here (an owner seat).
        from tools.income_rule.owner_verb import income_reply
        return income_reply(list(args[1:]), user_id)
    if args and args[0].lower() in ("signed", "signatures"):
        # 113 W6 G-5: what the wallet signed — a read of the one signing journal.
        return signing_history_reply(list(args[1:]))
    if args and args[0].lower() in ("tokens", "token", "trust", "untrust"):
        # W1: which tokens are trusted, and the owner's trust / untrust — the
        # chat seat that replaces `polyrob wallet pin-token` on the server.
        from core.open_positions import open_positions_db_path
        from core.wallet.token_trust import tokens_reply
        return tokens_reply(user_id, list(args),
                            positions_db=(open_positions_db_path(data_dir)
                                          if data_dir else None))
    if not args or args[0].lower() in ("overview", "accounts"):
        from core.wallet.view import wallet_view, render_wallet
        try:
            return render_wallet(wallet_view(user_id, data_dir=data_dir))
        except PermissionError as exc:
            return str(exc)
    want_balances = bool(args) and args[0].lower() in ("balances", "balance", "full")
    try:
        from core.wallet.factory import get_agent_wallet
        w = get_agent_wallet()
    except ValueError as e:
        return f"Wallet unavailable: {e}"
    except Exception as e:
        logger.debug("wallet read failed", exc_info=True)
        return f"Wallet unavailable: {e}"
    if w is None:
        # D72: name what the owner can DO. A flag name on a phone screen is
        # something he can only act on by opening a shell.
        return ("I have no wallet — one has not been enabled for me, so I "
                "hold no funds and can move none. Arm it with "
                "`polyrob config set AGENT_WALLET_ENABLED true` (it applies on "
                "my next restart).")

    cfg = w.config
    try:
        address = w.address
    except ValueError as e:
        return f"Wallet MISCONFIGURED: {e}"
    lines = [f"Wallet · network {cfg.network} · venue {w.operational_venue}",
             f"Fund: {_code(address)}"]
    # The Solana identity off the same seed (2026-08-27): the owner must see —
    # and be able to fund — this address from the same glance, or it stays
    # invisible until a Solana trade fails on an empty fee balance.
    #
    # ⚠️ D40: a derivation FAULT used to hide the line entirely, which reads
    # exactly like "this wallet has no Solana identity" — and the owner then
    # funds nothing and a Solana trade fails later on an empty fee balance. An
    # address we could not derive is named as unavailable, with the reason.
    try:
        sol_addr = w.solana_address
    except Exception as exc:
        logger.warning("solana address derivation failed", exc_info=True)
        sol_addr = None
        lines.append(f"Solana: unavailable ({type(exc).__name__}: "
                     f"{str(exc)[:80]}) — I could not derive it, which is not "
                     f"the same as not having one.")
    if sol_addr:
        lines.append(f"Solana: {_code(sol_addr)}")
    if want_balances:
        if cfg.network != "mainnet":
            lines.append("(balances are mainnet-only — network is "
                         f"{cfg.network})")
        else:
            lines.extend(_wallet_balance_lines(w))
    lines.extend(_cap_lines(user_id, cfg))
    if not want_balances:
        lines.append("/wallet balances reads the on-chain amounts.")
    lines.append(CAP_NOTE)
    return "\n".join(lines)


def _cap_lines(user_id: Optional[str], cfg: Any) -> List[str]:
    """The caps the GATE will actually apply, not the ones it was BUILT with.

    ⚠️ D16: ``cfg`` is the ``WalletConfig`` frozen when the process-wide wallet
    singleton was constructed. Since 2026-09-18 the live gate re-resolves both
    caps per spend (``PolicyGate._refresh_caps`` → ``effective_max_per_tx_usd``
    / ``effective_daily_cap_usd``), so an owner-approved raise applied
    immediately to SPENDING and never to this readout — the one screen he
    checks to confirm the raise landed. Read the same resolvers the gate does.

    Fail-open to the constructed values, LABELLED: a cap we could not re-resolve
    must not be printed as though it were the live one.
    """
    from core.runtime_paths import prefs_home_dir
    try:
        from core.wallet.config import (effective_daily_cap_usd,
                                        effective_max_per_tx_usd)
        home = prefs_home_dir()
        per_tx = effective_max_per_tx_usd(user_id, home)
        daily_value = effective_daily_cap_usd(user_id, home)
        stale = ""
    except Exception as exc:
        logger.warning("effective wallet caps unreadable", exc_info=True)
        per_tx, daily_value = cfg.max_per_tx_usd, cfg.daily_cap_usd
        stale = (f" ⚠️ these are the values I started with — I could not "
                 f"re-read the live ones ({type(exc).__name__})")
    daily = f"${daily_value:.2f}" if daily_value is not None else "UNLIMITED"
    out = [f"Caps: ${per_tx:.2f}/tx · daily {daily}{stale}"]
    if daily_value is None:
        out.append("⚠️ The per-tx cap is a catastrophic-loss ceiling, not a "
                   "budget, and the daily cap is unlimited.")
    if not stale:
        out.extend(_signer_bound_lines(user_id))
    return out


def _signer_bound_lines(user_id: Optional[str]) -> List[str]:
    """Where a setting sits above the signer's hard cap, name the cap that
    binds and where to raise it (the signer is the envelope). Fail-open: no
    signer, or no answer, is no line."""
    try:
        from core.runtime_paths import prefs_home_dir
        from core.wallet.config import configured_caps_now
        from core.wallet.signer_envelope import (bound_legs, bound_text, reported_caps,
                                                 signer_unanswered, unanswered_text)
        if signer_unanswered():
            # The zero clamp of a silent signer is not a cap to raise in
            # signer.toml: say the signer is down instead.
            text = unanswered_text()
            return ["⚠️ " + text[0].upper() + text[1:]]
        caps = reported_caps()
        if caps is None:
            return []
        legs = bound_legs(caps, configured_caps_now(user_id=user_id,
                                                    home_dir=prefs_home_dir()))
    except Exception:
        logger.debug("signer bound check failed", exc_info=True)
        return []
    return ["Bound by the signer — " + bound_text(b) for b in legs]


def _wallet_balance_lines(w) -> List[str]:
    """Per-venue on-chain balances, fail-open to a note (never a traceback)."""
    from core.wallet.onchain import VENUE_CHAIN, balances as _balances
    out = []
    for venue in FUNDABLE_VENUES:
        try:
            addr = w.signer_for(venue).address
            native, usdc = _balances(addr, VENUE_CHAIN[venue])
        except Exception:
            logger.debug("balance probe failed for %s", venue, exc_info=True)
            out.append(f"  {venue}: balance unavailable")
            continue
        u = f"{usdc:.2f}" if usdc is not None else "n/a"
        n = f"{native:.5f}" if native is not None else "n/a"
        out.append(f"  {venue}: USDC={u} gas={n}")
    try:
        sol_addr = w.solana_address
    except Exception:
        sol_addr = None
    if sol_addr:
        try:
            from core.wallet import chains as _chains
            from core.wallet import solana_onchain
            sol_bal = solana_onchain.native_balance(sol_addr)
            usdc_raw = None
            row = _chains.get("solana")
            if row and row.usdc:
                tb = solana_onchain.token_balances(sol_addr)
                usdc_raw = None if tb is None else tb.get(row.usdc, 0)
            u = f"{usdc_raw / 1e6:.2f}" if usdc_raw is not None else "n/a"
            s = f"{sol_bal:.5f}" if sol_bal is not None else "n/a"
            out.append(f"  solana: USDC={u} SOL={s}")
        except Exception:
            logger.debug("solana balance probe failed", exc_info=True)
            out.append("  solana: balance unavailable")
    return out



# ---------------------------------------------------------------------------
# /wallet signed (113 W6 G-5)
# ---------------------------------------------------------------------------

#: Rows one reply lists; the owner may ask for up to MAX_ROWS.
DEFAULT_ROWS = 10
MAX_ROWS = 50


def signing_history_reply(args: List[str]) -> str:
    """``/wallet signed [n]`` — the last *n* signatures, newest first."""
    try:
        limit = max(1, min(MAX_ROWS, int(args[0]))) if args else DEFAULT_ROWS
    except (TypeError, ValueError):
        return f"Usage: /wallet signed [n] (1-{MAX_ROWS}, default {DEFAULT_ROWS})"
    try:
        from tools.wallet_sign.store import WalletSignStore, store_path
        path = store_path()
        if not os.path.isfile(path):
            return "The wallet has signed nothing yet (there is no signing journal)."
        store = WalletSignStore(path)
        recent = store.recent(limit)
        creds = store.credentials()
    except Exception as exc:  # noqa: BLE001 — unreadable is not empty
        return (f"I could not read the signing journal ({type(exc).__name__}: "
                f"{str(exc)[:120]}). That is not 'nothing signed'.")
    lines = [f"Last {len(recent)} signature(s), newest first:" if recent
             else "The wallet has signed nothing yet."]
    for r in recent:
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(r["ts"])))
        lines.append(f"• {when}Z {r['origin']} — {r['kind']} ({r['scheme']}, class "
                     f"{r['cls']}{', on my own' if r['autonomous'] else ''})")
    if creds:
        lines.append(f"Credentials sites gave back ({len(creds)}, fingerprints only):")
        lines += [f"• {c['origin']} '{c['label']}' {c['fingerprint']}" for c in creds]
    return "\n".join(lines)



__all__ = ["CAP_NOTE", "DEFAULT_ROWS", "MAX_ROWS", "set_autonomous_ceiling",
           "signing_history_reply", "wallet_reply"]
