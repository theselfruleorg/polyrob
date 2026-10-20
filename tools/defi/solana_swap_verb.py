"""``defi_trade.solana_swap`` — swap SPL tokens on Solana via Jupiter.

Moved out of ``trade_tool.py`` (W9 god-file split, behaviour-preserving). The
tool keeps a thin delegating method (which also holds the default-off flag
check); module-level helpers are imported from ``tools.defi.trade_tool`` at call
time so a test that patches them there still reaches this code.

The verb runs as ordered phases (``perform_solana_swap`` at the bottom). Each
phase returns None to continue or a result to stop, and passes what the next
phase needs on one namespace. The phase ORDER is the security order: identity
before the quote, the simulation before the inflow check, every assertion
before PolicyGate and the broadcast.
"""
from __future__ import annotations

import asyncio
import logging
import math
import types
from typing import Optional

logger = logging.getLogger("tools.defi.trade_tool")


#: 085 L3: the Token-2022 extensions that trap a BOUGHT position. The send
#: verb's ``extension_refusal`` judges their active state (a fee > 0, a hook
#: program, default-frozen, paused); ``nonTransferable`` always refuses;
#: ``permanentDelegate`` is benign for a send (we give the token away) but on a
#: buy its delegate can move our position out at any time.
_BUY_TRAP_EXTENSIONS = frozenset({"transferFeeConfig", "transferHook",
                                  "defaultAccountState", "pausableConfig"})


def _read_mint(mint: str):
    """The mint account, ``jsonParsed``, from the wallet's Solana RPC (seam)."""
    from tools.defi.solana_send_verb import _rpc
    return _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])


def mint_trap_refusal(mint: str) -> Optional[str]:
    """Why buying ``mint`` is refused on its ON-CHAIN extensions, or None.

    Refuses an active permanent delegate, transfer hook, transfer fee > 0, a
    default-frozen account state, a nonTransferable mint or a paused
    pausableConfig, regardless of any screen. A mint that cannot
    be read is refused too: a check that did not run is not a pass."""
    from core.wallet import spl_token
    from tools.defi.solana_send_verb import extension_refusal
    try:
        state = spl_token.decode_mint_account(_read_mint(mint))
    except Exception as exc:
        return f"could not read the mint {mint} on-chain ({exc}), so its extensions are unknown"
    if state is None or state.get("type") not in (None, "mint"):
        return f"{mint} does not read on-chain as an SPL mint"
    exts = tuple(state.get("extensions") or ())
    for name, ext_state in exts:
        if name == "nonTransferable":
            return ("the mint is nonTransferable — a bought position could never "
                    "be sold or moved")
        if name == "permanentDelegate" and (ext_state or {}).get("delegate"):
            return (f"the mint carries an active permanentDelegate "
                    f"({(ext_state or {}).get('delegate')}) — it can move a bought "
                    f"position out of our account at any time")
    return extension_refusal(tuple(e for e in exts if e[0] in _BUY_TRAP_EXTENSIONS))


async def _prepare(tool, params, execution_context, s):
    """Inputs, the wallet and its gate, sizing, and the turn gate (tx_guard steps 1-2)."""
    from tools.defi.trade_tool import _max_slippage_bps  # call-time
    from core.wallet.addresses import normalize_for_chain

    try:
        token_in = normalize_for_chain("solana", params.token_in)
        token_out = normalize_for_chain("solana", params.token_out)
    except ValueError as exc:
        return tool._ar(error=str(exc))
    if token_in == token_out:
        return tool._ar(error="token_in and token_out are the same token")

    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT)
    try:
        signer = wallet.solana_signer()
    except Exception as exc:
        return tool._ar(error=f"no Solana signer: {exc}")
    gate = getattr(wallet, "policy", None)
    if gate is None:
        return tool._ar(error=(
            "refused: the wallet exposes no PolicyGate — a money verb may "
            "not run ungoverned"))

    slippage = params.slippage_bps or _max_slippage_bps()
    # Jupiter quotes in RAW units; the caller gives a human amount, and we
    # have no on-chain decimals reader for SPL yet, so the caller's amount
    # is treated as already-scaled by the mint's decimals via token_info.
    ident = await asyncio.to_thread(tool._identity_solana, token_in)
    if ident is None:
        return tool._ar(error=(
            f"cannot read decimals for {token_in} — refusing to size a swap "
            f"against a token whose denomination is unknown. A guessed "
            f"denomination misprices a trade by orders of magnitude."))
    from core.wallet.tokens import raw_amount
    try:
        amount_in_raw = raw_amount(params.amount_in, ident)
        if not 0 < amount_in_raw < 2 ** 64:
            raise ValueError("Solana swap amount is outside a u64")
    except ValueError as exc:
        return tool._ar(error=f"refused: {exc}")

    # Held balance of the outflow token, read LAZILY (only the exit lanes
    # and the unpriceable-valuation ladder need it) and memoized.
    _held_cache: dict = {}

    def _held_raw():
        if "v" not in _held_cache:
            _held_cache["v"] = tool._solana_held_raw(signer.address, token_in)
        return _held_cache["v"]

    usdc_mint = tool._solana_usdc_mint()

    def _exit_shaped():
        # A sell of the held token, within the held balance, into the
        # chain's pinned quote asset — the Solana mirror of
        # tx_guard._intent_is_exit_shaped's swap arm.
        if not usdc_mint or token_out != usdc_mint or token_in == usdc_mint:
            return False
        held = _held_raw()
        return held is not None and amount_in_raw <= held

    # tx_guard steps 1-2, mirrored: owner kill-switch, then turn origin
    # (forged/leaf refusal, the DEFI_AUTONOMOUS_TURN_TRADING goal lane, and
    # the DEFI_MONITOR_EXITS exit-only carve-out).
    refusal, autonomous_origin, monitor_exit = await asyncio.to_thread(tool._solana_turn_gate,
        execution_context, exit_shaped_fn=_exit_shaped)
    if refusal:
        return tool._ar(error=refusal)
    s._exit_shaped = _exit_shaped
    s._held_raw = _held_raw
    s.amount_in_raw = amount_in_raw
    s.autonomous_origin = autonomous_origin
    s.gate = gate
    s.ident = ident
    s.monitor_exit = monitor_exit
    s.signer = signer
    s.slippage = slippage
    s.token_in = token_in
    s.token_out = token_out
    s.usdc_mint = usdc_mint
    return None


async def _screen_buy(tool, params, execution_context, s):
    """The run's target and the bought mint's screen and identity (CR-L10, 068 G1)."""
    from tools.defi.trade_tool import _SOLANA_BLOCKING_FLAGS, _WSOL_MINT  # call-time
    token_in, token_out, usdc_mint = s.token_in, s.token_out, s.usdc_mint

    # CR-L10: screen what is being BOUGHT before anything is quoted. The
    # pinned quote assets (USDC, wSOL) are the chain's own and are not
    # screened — USDC is freezable by design. Any other mint is refused on
    # an ACTIVE Token-2022 trap (a transfer fee skimmed on every move, a
    # hook program that can refuse the sell, an account born frozen, a
    # non-transferable or permanently-delegated mint), and refused when the
    # screen could not run: a screen that did not run is not a pass.
    # 068 B4: the run's declared target covers every output, including the
    # canonical ones — USDC -> wSOL is a buy; only a held non-canonical
    # token sold into USDC/wSOL is an exit.
    from core.wallet.buy_target import acquisition_refusal
    _why = acquisition_refusal(execution_context, chain="solana",
                               token_out=token_out, token_in=token_in)
    if _why:
        return tool._ar(error=_why)
    _ident = None  # the bought mint's identity; canonical outputs have none
    if token_out not in (usdc_mint, _WSOL_MINT):
        try:
            verdict = await asyncio.to_thread(tool._solana_screen, token_out)
        except Exception:
            verdict = None
        if verdict is None or not getattr(verdict, "available", False):
            return tool._ar(error=(
                f"refused: the token screen for {token_out} is UNAVAILABLE, "
                f"and a screen that did not run is not a pass. Retry, or "
                f"have the owner decide. Nothing was quoted or broadcast."))
        blocking = sorted(set(getattr(verdict, "flags", ()) or ())
                          & _SOLANA_BLOCKING_FLAGS)
        if getattr(verdict, "missing", ()):
            return tool._ar(error="refused: the token screen is PARTIAL; missing safety checks must run before buying")
        if blocking:
            return tool._ar(error=(
                f"refused: {token_out} carries {', '.join(blocking)} — a "
                f"position in it may be skimmed, frozen or unsellable. "
                f"Nothing was quoted or broadcast."))
        # 085 L3: the screen is a third party. The mint's own extensions are
        # read from the chain and refuse the trap shapes whatever it said.
        why = await asyncio.to_thread(mint_trap_refusal, token_out)
        if why:
            return tool._ar(error=f"refused: {why}. Nothing was quoted or broadcast.")
        # 068 G1 (Solana): WHICH mint this buys. The symbol/name are the
        # mint's self-reported metadata from the same screen; verified means
        # owner-pinned (USDC/wSOL never reach this branch). The identity
        # gate runs before the quote, so it sees UNAVAILABLE: an untrusted
        # buy is limited to the scouting ticket; a trusted one is route-
        # checked below against the exit-grade price.
        from core.wallet.token_pins import owner_pin
        from tools.defi.identity_gate import buy_identity_refusal, container_of
        from core.wallet.token_pins import PinStoreUnreadable
        try:
            _pinned = owner_pin("solana", token_out, strict=True) is not None
        except PinStoreUnreadable:
            _pinned = False  # the gate below re-reads strictly and refuses
        except Exception:
            _pinned = False
        _ident = types.SimpleNamespace(
            address=token_out, symbol=getattr(verdict, "symbol", None),
            name=getattr(verdict, "name", None), verified=_pinned)
        _why = buy_identity_refusal(
            chain="solana", token_out=token_out, id_out=_ident,
            max_spend_usd=params.max_spend_usd, route_verdict="UNAVAILABLE",
            execution_context=execution_context,
            container=container_of(tool))
        if _why:
            return tool._ar(error=_why)
    s._ident = _ident
    return None


async def _quote_and_build(tool, params, execution_context, s):
    """The Jupiter quote, its floor, the route sanity check, and the built transaction."""
    _exit_shaped = s._exit_shaped
    _held_raw = s._held_raw
    _ident = s._ident
    amount_in_raw = s.amount_in_raw
    ident = s.ident
    signer = s.signer
    slippage = s.slippage
    token_in = s.token_in
    token_out = s.token_out

    quote = await asyncio.to_thread(tool._solana_quote, token_in, token_out,
                                    amount_in_raw, slippage)
    if quote is None:
        return tool._ar(error=(
            f"no route for {token_in} -> {token_out} on solana, or the "
            f"lookup failed. Either way this is UNKNOWN, not a zero-value "
            f"trade — retry before concluding the token is unreachable."))

    # CR-H03: the quote is an untrusted third-party document. It must be
    # a quote for THIS request — same mints, same input amount — or its
    # floor is a floor for some other trade.
    if (str(getattr(quote, "token_in", "")) != token_in
            or str(getattr(quote, "token_out", "")) != token_out
            or int(getattr(quote, "amount_in_raw", -1) or -1) != amount_in_raw):
        return tool._ar(error=(
            f"refused: the Jupiter quote does not match the request — it "
            f"quotes {getattr(quote, 'token_in', None)} -> "
            f"{getattr(quote, 'token_out', None)} for "
            f"{getattr(quote, 'amount_in_raw', None)} raw, but the request "
            f"is {token_in} -> {token_out} for {amount_in_raw} raw. "
            f"Nothing was built."))

    from tools.defi.providers import jupiter
    floor = jupiter.verified_floor(quote.raw, slippage_bps=slippage)
    if floor is None:
        return tool._ar(error=(
            f"refused: Jupiter's own minimum output does not clear your "
            f"{slippage}bps slippage bound. Jupiter bakes its minimum into "
            f"the transaction it builds, so a looser one cannot be "
            f"rewritten — only refused."))

    if params.minimum_output_raw is not None and floor < params.minimum_output_raw:
        return tool._ar(error="The minimum received fell below the confirmed quote; nothing was built")

    out_decimals = await asyncio.to_thread(tool._identity_solana, token_out)
    exit_held = await asyncio.to_thread(lambda: _held_raw() if _exit_shaped() else None)
    # DEFI-6 (as on EVM): a TRUSTED buy (owner pin, own launch, owner
    # target) is checked against the liquidity-backed exit-grade price, so
    # it is not held to the unchecked ticket; a lying quote still DISAGREES.
    from tools.defi.identity_gate import trusted_buy
    out_trusted = bool(_ident is not None and await asyncio.to_thread(
        trusted_buy, _ident, chain="solana", token_out=token_out,
        execution_context=execution_context))
    sanity_verdict, sanity_note = await asyncio.to_thread(
        tool._route_sanity, "solana", quote,
        types.SimpleNamespace(decimals=ident, symbol=token_in),
        types.SimpleNamespace(decimals=out_decimals, symbol=token_out),
        slippage_bps=slippage, held_balance_raw=exit_held,
        out_trusted=out_trusted)
    if sanity_verdict == "DISAGREES":
        return tool._ar(error=f"refused: {sanity_note}")
    from tools.defi.buy_screen import (is_sell_into_quote, owner_confirms,
                                       unchecked_route_refusal,
                                       unpriced_sell_owner_queue)
    from tools.defi.trade_tool import _WSOL_MINT  # call-time
    _quote_assets = {m for m in (s.usdc_mint, _WSOL_MINT) if m}
    if not params.dry_run:
        # 085 EV-2 (Solana): a sell of a non-quote mint into USDC/wSOL tests
        # trust on the mint SOLD; untrusted + unpriced above the ticket goes
        # to the owner (D5) — before 085 it was a plain refusal here.
        if is_sell_into_quote(token_out in _quote_assets, token_in in _quote_assets):
            in_trusted = await asyncio.to_thread(
                trusted_buy, types.SimpleNamespace(address=token_in, verified=False),
                chain="solana", token_out=token_in,
                execution_context=execution_context)
            why = unpriced_sell_owner_queue(
                sanity_verdict, params.max_spend_usd, trusted_in=in_trusted,
                owner_ok=owner_confirms(tool, execution_context, params))
        else:
            why = unchecked_route_refusal(sanity_verdict, params.max_spend_usd,
                                          trusted=out_trusted)
        if why:
            return tool._ar(error=why)

    raw_tx = await asyncio.to_thread(tool._solana_build, quote, signer.address)
    if raw_tx is None:
        return tool._ar(error=(
            "Jupiter could not build a transaction for this route (often an "
            "unfunded or unexpected token-account state). Nothing was sent."))
    s.floor = floor
    s.quote = quote
    s.raw_tx = raw_tx
    return None


async def _simulate_and_assert(tool, params, execution_context, s):
    """The vetted simulation and every delta assertion (tx_guard step 6 mirror)."""
    from tools.defi.trade_tool import _WSOL_MINT  # call-time
    from core.wallet.solana_simulation import is_plausible_rent
    amount_in_raw = s.amount_in_raw
    floor = s.floor
    monitor_exit = s.monitor_exit
    raw_tx = s.raw_tx
    signer = s.signer
    slippage = s.slippage
    token_in = s.token_in
    token_out = s.token_out

    from core.wallet.solana_swap_bounds import SwapBounds
    deltas = await asyncio.to_thread(
        lambda: tool._solana_simulate(raw_tx=raw_tx, owner=signer.address,
                                      mints=(token_in, token_out),
                                      swap_bounds=SwapBounds(token_in, token_out,
                                                             amount_in_raw, floor, slippage)))
    if deltas is None or not deltas.ok:
        reason = getattr(deltas, "reason", "no result") if deltas else "no result"
        return tool._ar(error=(
            f"refused: the simulation did not pass ({reason}). A simulation "
            f"that did not run is not a simulation that passed."))

    # The Solana replacement for the undeclared-Approval refusal. A swap
    # grants nothing: any delegate, close authority, ownership change or
    # freeze the simulation reveals is undeclared by definition.
    if deltas.grants_authority():
        return tool._ar(error=(
            f"refused: this transaction changes AUTHORITY over your "
            f"accounts — {list(deltas.authority_grants)}. A swap grants "
            f"nothing; an authority change is a future drain the swap "
            f"itself does not perform. Nothing was broadcast."))

    if any(delta < 0 for mint, delta in (deltas.token_deltas or {}).items()
           if mint != token_in):
        return tool._ar(error=("refused: simulation shows an undeclared token outflow "
                               "beside the swap input. Nothing was broadcast."))

    # 068 R3-1: under a declared target, EVERY mint the simulation shows
    # arriving in our accounts counts, not only the named token_out — a
    # route that also drops another token into the wallet acquires it.
    from core.wallet.buy_target import net_inflow_refusal
    _extra = net_inflow_refusal(execution_context, chain="solana",
                                net_moves=dict(deltas.token_deltas or {}))
    if _extra:
        return tool._ar(error=_extra)

    # A swap that moves NO tokens is not a swap. Passing here would be
    # passing because we observed nothing, which is indistinguishable from
    # passing because nothing moved — and the first is a broken simulation.
    if not deltas.token_deltas:
        return tool._ar(error=(
            "refused: the simulation observed NO token movement for this "
            "swap. A swap that moves nothing is not a swap, and an empty "
            "delta set cannot be told apart from an unobserved one. "
            "Nothing was broadcast."))

    # Selling SOL: the native move IS the swap, so it is measured, not
    # feared. The wallet holds NATIVE SOL and Jupiter wraps it inside this
    # very transaction, closing the temporary wSOL account again — so the
    # outflow lands in `native_delta` and the wSOL token account nets to
    # nothing, often never appearing in `token_deltas` at all. Read
    # literally, the two guards below then refuse a legitimate sell twice
    # over: the rent classifier sees ~0.93 SOL as a drain, and the
    # unobserved-mint rule sees no wSOL observation (live, prod
    # 2026-09-08). `_solana_held_raw` already folds native SOL into the
    # wSOL balance for exactly this reason; the outflow side folds the
    # same way. A partially-wrapped wallet spends from BOTH, so both are
    # summed. This is an OBSERVATION, never a licence — the folded outflow
    # still faces the declared-amount bound below, identically.
    selling_sol = token_in == _WSOL_MINT
    if selling_sol:
        outflow_raw = -(deltas.native_delta
                        + deltas.token_deltas.get(_WSOL_MINT, 0))
        if outflow_raw <= 0:
            return tool._ar(error=(
                f"refused: you declared a sale of SOL, but the simulation "
                f"shows no SOL leaving — native delta "
                f"{deltas.native_delta} lamports, wSOL delta "
                f"{deltas.token_deltas.get(_WSOL_MINT, 0)}. An outflow of "
                f"nothing is not a swap, and it cannot be told apart from "
                f"an unobserved one. Nothing was broadcast."))
    else:
        # Rent is classified, not licensed (Solana research mismatch #5).
        # Armed for every non-SOL sell: an unexplained native outflow
        # beside a token swap is the drain this exists to catch.
        if not is_plausible_rent(deltas.native_delta):
            return tool._ar(error=(
                f"refused: the transaction moves {deltas.native_delta} "
                f"lamports of SOL, far more than account rent and fees "
                f"explain. Nothing was broadcast."))

        # An UNOBSERVED declared mint fails CLOSED. `token_deltas` being
        # non-empty only says SOMETHING moved; if the declared outflow mint
        # is not in it, the outflow assertion below simply does not run,
        # and a check that did not run must never read as a check that
        # passed. That was live: a Token-2022 mint's associated account was
        # derived under the classic SPL Token program, so the simulation
        # observed no state for it and `in_delta` came back None — silently
        # skipping the one assertion that bounds how much may leave.
        if token_in not in deltas.token_deltas:
            return tool._ar(error=(
                f"refused: the simulation could not observe token "
                f"{token_in} — it moved {list(deltas.token_deltas)} but "
                f"says nothing about the token you declared as leaving. "
                f"The outflow assertion cannot run against an unobserved "
                f"account, and a check that did not run is not a check "
                f"that passed. Nothing was broadcast."))
        in_delta = deltas.token_deltas.get(token_in)
        outflow_raw = -in_delta if (in_delta is not None
                                    and in_delta < 0) else 0

    # Delta assertion, sell side (tx_guard step 6 mirror): the measured
    # outflow of token_in may not exceed the declared amount (0.1% + 1 raw
    # tolerance for routing dust). Applies to the folded SOL outflow too —
    # folding changes what is OBSERVED, never what is PERMITTED.
    #
    # ⚠️ SOL-input only: the fee is the SAME ASSET as the principal. The
    # non-SOL branch reads its outflow from `token_deltas` (which never
    # carries a fee) and classifies native movement separately through
    # `is_plausible_rent`. A native sell cannot separate them by asset, so
    # the folded outflow is ALWAYS amount_in + fee + rent — measured at
    # 3,670,479 lamports on prod. Against a 0.1% tolerance that needs a
    # ~3,670 SOL swap to clear, so this refused 100% of SOL-input swaps at
    # every size the treasury could trade (live: goal e9c585d9, 2026-09-09,
    # "unsatisfiable at any size" — an accurate report of a real defect).
    # So classify the excess the same way the other branch classifies
    # native movement. Still a CLASSIFICATION, never a licence: the excess
    # is capped at MAX_PLAUSIBLE_RENT_LAMPORTS (0.01 SOL), a gross
    # overspend refuses exactly as before, and the USD ceiling below
    # (max_spend_usd / DEFI_AUTONOMOUS_MAX_USD / PolicyGate) is untouched.
    if outflow_raw > 0:
        if outflow_raw > amount_in_raw + max(1, amount_in_raw // 1000):
            excess = outflow_raw - amount_in_raw
            if not (selling_sol and is_plausible_rent(-excess)):
                unit = ("lamports of SOL" if selling_sol
                        else f"raw of {token_in}")
                return tool._ar(error=(
                    f"refused: the simulation shows {outflow_raw} {unit} "
                    f"leaving, but only {amount_in_raw} was declared "
                    f"— the transaction does more than the declared swap. "
                    f"Nothing was broadcast."))

    # CR-L06: the rent classifier above is a coarse ceiling (0.01 SOL),
    # not an account of where native SOL went. The exact account is the
    # fee plus the rent retained in accounts this transaction creates and
    # we own; whatever native outflow those do not explain left the
    # wallet, and it is CHARGED to the USD caps below. Unknown fee = the
    # account cannot be drawn up, so refuse.
    fee_lamports = getattr(deltas, "fee_lamports", None)
    if fee_lamports is None:
        return tool._ar(error=(
            "refused: the simulation did not compute this transaction's "
            "fee, so native SOL leaving cannot be told apart from fee and "
            "rent. Nothing was broadcast."))
    retained = int(getattr(deltas, "retained_rent_lamports", 0) or 0)
    if selling_sol:
        # The principal is native too; the declared amount is subtracted.
        native_excess = max(0, outflow_raw - amount_in_raw
                            - int(fee_lamports) - retained)
    else:
        native_excess = max(0, -int(deltas.native_delta)
                            - int(fee_lamports) - retained)

    out_delta_raw = deltas.token_deltas.get(token_out)
    if monitor_exit and not (out_delta_raw and out_delta_raw > 0):
        return tool._ar(error=(
            "refused: the monitor-exit lane requires a measured inflow of "
            "the declared receive token — the simulation shows none, so "
            "this is not an exit. Nothing was broadcast."))

    # CR-H03: assert what ARRIVES, not only what leaves. The floor was
    # checked against the quote JSON only; the simulation is the evidence.
    # Buying SOL: Jupiter unwraps inside the transaction, so the receipt
    # lands as native (plus any wSOL left wrapped); the fee is paid from the
    # same balance, so it is added back when the simulation computed it.
    if token_out == _WSOL_MINT:
        _fee = getattr(deltas, "fee_lamports", None) or 0
        received_raw = (deltas.native_delta
                        + deltas.token_deltas.get(_WSOL_MINT, 0) + _fee)
    elif out_delta_raw is None:
        return tool._ar(error=(
            f"refused: the simulation could not observe the token you are "
            f"buying ({token_out}) — it moved {list(deltas.token_deltas)}. "
            f"A receipt that was not observed is not a receipt that "
            f"arrived. Nothing was broadcast."))
    else:
        received_raw = out_delta_raw
    # The same 5 bp grace the instruction bound allows (solana_swap_bounds):
    # one bound for both checks, so nothing further below the floor slips.
    from core.wallet.solana_swap_bounds import min_acceptable
    if received_raw < min_acceptable(floor):
        return tool._ar(error=(
            f"refused: the simulation delivers {received_raw} raw of "
            f"{token_out}, below the route's floor {floor}. A swap that "
            f"takes {token_in} and returns less than its minimum is not "
            f"the trade that was quoted. Nothing was broadcast."))
    s.deltas = deltas
    s.fee_lamports = fee_lamports
    s.native_excess = native_excess
    s.out_delta_raw = out_delta_raw
    s.retained = retained
    return None


async def _value(tool, params, execution_context, s):
    """The USD valuation and the declared-ceiling check (tx_guard step 7 mirror)."""
    from tools.defi.trade_tool import _WSOL_MINT  # call-time
    _held_raw = s._held_raw
    amount_in_raw = s.amount_in_raw
    deltas = s.deltas
    fee_lamports = s.fee_lamports
    floor = s.floor
    native_excess = s.native_excess
    out_delta_raw = s.out_delta_raw
    quote = s.quote
    retained = s.retained
    token_in = s.token_in
    token_out = s.token_out
    usdc_mint = s.usdc_mint

    # Valuation (tx_guard step 7 mirror): price the outflow, or — for an
    # exit within the held balance that sells into the chain's USDC — value
    # it at the simulation's measured receipt. Unpriceable refuses; no cap
    # can bound a number you do not have. Caps operate in cents.
    valuation = {"valuation_basis": "outflow"}
    amount_usd = await asyncio.to_thread(tool._solana_value_usd,
        token_in=token_in, amount_in=params.amount_in, token_out=token_out,
        out_delta_raw=out_delta_raw, usdc_mint=usdc_mint,
        exit_bounded_fn=lambda: (_held_raw() is not None
                                 and amount_in_raw <= _held_raw()),
        valuation=valuation)
    if amount_usd is None:
        return tool._ar(error=(
            "refused: the outflow could not be valued in USD by any "
            "source, so no cap can bound it. A sell of a held token into "
            "the chain's USDC is valued at the simulation's measured "
            "receipt; anything else refuses. Nothing was broadcast."))
    native_cost = native_excess + int(fee_lamports) + retained
    # The declared max_spend_usd asserts the swap's VALUE (the outflow plus any
    # SOL it moves beyond fees); the network fee and retained rent are charged
    # to the PolicyGate caps only — EVM parity (tx_guard, cbf59c857). Charging
    # them to the declared max refused every swap declared at exactly its value.
    value_usd = amount_usd
    if native_cost > 0:
        try:
            sol_px = await asyncio.to_thread(tool._price, "solana", _WSOL_MINT)
        except Exception:
            sol_px = None
        if not sol_px or not math.isfinite(sol_px) or sol_px <= 0:
            return tool._ar(error=(
                f"refused: {native_cost} lamports of SOL (including fees and "
                f"retained rent) have no trustworthy price "
                f"to charge them against the caps. Nothing was broadcast."))
        value_usd += native_excess / 1e9 * float(sol_px)
        amount_usd += native_cost / 1e9 * float(sol_px)
    from core.money.valuation import usd_ceiling
    amount_usd = usd_ceiling(amount_usd)
    declared = float(params.max_spend_usd)
    if usd_ceiling(value_usd) > declared:
        return tool._ar(error=(
            f"refused: ${usd_ceiling(value_usd):.2f} exceeds the declared "
            f"max_spend_usd ${declared:.2f}. Nothing was broadcast."))

    header = (f"solana swap {params.amount_in} {token_in} -> {token_out}\n"
              f"  route: {quote.venue}\n"
              f"  quoted out: {quote.amount_out_raw}  (min {floor})\n"
              f"  simulated: token deltas {deltas.token_deltas}, "
              f"native {deltas.native_delta} lamports "
              f"(fee {fee_lamports}, charged excess {native_excess})\n"
              f"  valued: ${amount_usd:.2f} (declared max ${declared:.2f})\n")
    s.amount_usd = amount_usd
    s.header = header
    s.valuation = valuation
    return None


async def _broadcast(tool, params, execution_context, s):
    """PolicyGate, the autonomous ceiling, and the broadcast, under gate.reserve()."""
    from tools.defi.trade_tool import _NOT_SENT_OWNER_QUEUE, _turn_id  # call-time
    amount_in_raw = s.amount_in_raw
    amount_usd = s.amount_usd
    autonomous_origin = s.autonomous_origin
    floor = s.floor
    gate = s.gate
    header = s.header
    monitor_exit = s.monitor_exit
    raw_tx = s.raw_tx
    signer = s.signer
    token_in = s.token_in
    token_out = s.token_out
    valuation = s.valuation

    from core.wallet import tx_guard
    # Bind the economic intent, not the aggregator's fresh blockhash.
    import hashlib as _hashlib
    idem = "defi_solana_swap:" + _hashlib.sha256(
        f"{token_in}:{token_out}:{amount_in_raw}:{_turn_id(execution_context)}".encode()
    ).hexdigest()[:32]
    # reserve() spans check -> broadcast -> record so two concurrent money
    # verbs cannot both clear a nearly-exhausted cap (EVM parity).
    async with gate.reserve():
        # tx_guard step 0 mirror: a genuine owner turn is not bound by the
        # pause or the autonomous ceiling; an owner grant for this call
        # clears the ceiling. The hard caps below bind everyone.
        from core.money.ledger import pause_probe
        from tools.controller.turn_origin import _is_forged_or_autonomous_turn
        _owner_direct, _owner_granted = tx_guard.owner_authority(
            execution_context, _is_forged_or_autonomous_turn, tool, params)
        # PolicyGate: the pause (fail-closed inside check), per-tx
        # ceiling, rolling daily + venue caps, replay guard.
        with pause_probe((lambda: False) if _owner_direct else tx_guard._halted):
            verdict = gate.check(venue="defi", amount_usd=amount_usd,
                                 idempotency_key=idem)
        if not verdict.allowed:
            return tool._ar(content=header + (
                f"  guard: refused by PolicyGate: {verdict.reason}\n"
                f"  RESULT: NOT SENT — nothing was broadcast."))
        # tx_guard step 9 mirror: the autonomous ceiling and the
        # daily-cap-required bar for unattended origins.
        _ceiling = tx_guard.autonomous_max_usd(
            *tx_guard.ceiling_scope(execution_context))
        if amount_usd > _ceiling and not _owner_direct and not _owner_granted:
            return tool._ar(content=header + (
                f"  guard: owner approval required: ${amount_usd:.2f} is "
                f"above the autonomous ceiling "
                f"${_ceiling:.2f}\n"
                f"  lane:  owner_queue\n" + _NOT_SENT_OWNER_QUEUE))
        if autonomous_origin and not getattr(gate, "has_daily_cap", False):
            return tool._ar(content=header + (
                "  guard: refused — unattended trading needs an aggregate "
                "damage bound; set WALLET_DAILY_CAP_USD\n"
                "  lane:  owner_queue\n" + _NOT_SENT_OWNER_QUEUE))
        _lane = ("owner_direct" if _owner_direct
                 else "owner_approved" if _owner_granted else "autonomous")
        header += f"  lane:  {_lane}\n"
        if monitor_exit:
            logger.info(
                "defi.solana_swap monitor_exit token_in=%s token_out=%s "
                "amount_usd=%.2f — forged turn allowed for an EXIT-shaped "
                "swap (DEFI_MONITOR_EXITS)", token_in, token_out, amount_usd)
        if params.dry_run:
            return tool._ar(content=header + "\n[DRY RUN] simulation only — nothing was broadcast, queued or staged.",
                            metadata={"min_out_raw": floor, **valuation})

        # RPC trust (tx_guard step 4 mirror): the simulation, the deltas
        # and the caps all read from the RPC, so the shared public endpoint
        # cannot be the trust anchor for a broadcast. Dry runs above are a
        # $0 read and stay available unpinned.
        import os as _os
        if not _os.getenv("DEFI_SOLANA_RPC", "").strip():
            return tool._ar(error=(
                "refused: no pinned RPC for solana. The simulation, the "
                "deltas and the caps all read from it, so the shared "
                "public endpoint cannot be the trust anchor for moving "
                "funds — set DEFI_SOLANA_RPC. Dry runs are unaffected."))

        # CR-M05: Jupiter chose the blockhash. Sign only a transaction whose
        # blockhash the pinned RPC says is still valid — an expired or
        # unknown one either never lands (and reads as UNKNOWN for a
        # minute) or was never a live blockhash at all. Fail closed.
        try:
            bh_ok, bh_detail = await asyncio.to_thread(
                tool._solana_blockhash_valid, raw_tx)
        except Exception as exc:
            bh_ok, bh_detail = False, f"check failed: {exc}"
        if not bh_ok:
            return tool._ar(error=(
                f"refused: the transaction's recent blockhash is not "
                f"valid on the pinned RPC ({bh_detail}). Nothing was "
                f"signed or broadcast — re-quote and try again."))

        try:
            signature = await asyncio.to_thread(tool._solana_send, raw_tx, signer)
        except Exception as exc:
            from core.wallet.broadcast.evm import broadcast_failure_text
            return tool._ar(error=broadcast_failure_text(exc))
        if _lane != "autonomous" and hasattr(gate, "note_lane"):
            gate.note_lane(idem, _lane)
        gate.record(venue="defi", action="solana_swap",
                    amount_usd=amount_usd, counterparty=token_out,
                    idempotency_key=idem, result_ref=signature,
                    chain="solana")
        from core.wallet import tx_notify
        _used, _limit = tx_notify.caps_from_gate(gate)
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="solana_swap", route="solana", chain="solana", usd=amount_usd,
                tx_ref=signature, cap_used_usd=_used, cap_limit_usd=_limit),
            settled=False)
    s.header = header
    s.signature = signature
    return None


async def _confirm(tool, params, execution_context, s):
    """Confirmation, outside the reservation, and the result text."""
    from core.wallet import tx_notify
    amount_usd, header, signature = s.amount_usd, s.header, s.signature

    # Confirmation (the EVM `await_receipt` mirror). Deliberately OUTSIDE
    # the reservation: polling can take a minute, the spend is already
    # recorded, and holding the cap reservation that long would block every
    # other money verb. `BROADCAST: <sig>` on its own was a claim about a
    # transaction nobody had checked — on Solana "not confirmed" is far
    # more often "the blockhash expired" than "still pending", and a
    # revert is a landed FAILURE that still paid the fee. Never resend.
    # Off the event loop: the rail polls with a blocking `time.sleep`, and
    # a minute of stalled loop would freeze every other session's turn.
    import asyncio as _asyncio

    from core.wallet.solana_rail import confirmation_outcome
    try:
        confirmed, detail = await _asyncio.to_thread(
            tool._solana_confirm, signature)
    except Exception as exc:
        confirmed, detail = False, f"status read failed: {exc}"
    outcome = confirmation_outcome(confirmed, detail)
    tool._notify_tx(execution_context, tx_notify.TxNotice(
        verb="solana_swap", route="solana", chain="solana", usd=amount_usd, tx_ref=signature,
        state={"confirmed": tx_notify.STATE_CONFIRMED,
               "reverted": tx_notify.STATE_REVERTED}.get(
            outcome, tx_notify.STATE_IN_FLIGHT),
        detail=detail, ledger_recorded=True), settled=True)
    if outcome == "confirmed":
        return tool._ar(content=header + (
            f"\n  RESULT: CONFIRMED ({detail})\n  sig: {signature}"))
    if outcome == "reverted":
        return tool._ar(content=header + (
            f"\n  RESULT: REVERTED ON-CHAIN — it did NOT happen, but the "
            f"fee was spent.\n  sig: {signature}\n  detail: {detail}"))
    return tool._ar(content=header + (
        f"\n  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. It "
        f"may still land, or the blockhash may have expired — do NOT retry "
        f"blindly; check the signature first.\n  sig: {signature}\n"
        f"  detail: {detail}"))


async def perform_solana_swap(tool, params, execution_context=None):
    """The first Solana path that can move value.

    Every gate the EVM verbs carry, re-expressed for a chain with no
    allowances: a default-off flag, dry_run true by default, a declared USD
    ceiling asserted against the valued outflow, simulation + delta
    assertion, the AUTHORITY taxonomy in place of an allowance check, the
    fee-payer perimeter in the signer, the kill-switch + turn-origin bars,
    and the same PolicyGate caps (per-tx ceiling, rolling daily cap, replay
    guard, autonomous owner-queue lane) — recorded to the same audit.
    There is no EVM transaction here, so `tx_guard.authorize` cannot run;
    this verb mirrors its steps in the same order instead.
    """
    s = types.SimpleNamespace()  # what one phase hands the next
    for phase in (_prepare, _screen_buy, _quote_and_build, _simulate_and_assert,
                  _value, _broadcast):
        refused = await phase(tool, params, execution_context, s)
        if refused is not None:
            return refused
    return await _confirm(tool, params, execution_context, s)
