"""``defi_trade.solana_transfer`` — send SOL or an SPL token on Solana.

The Solana twin of ``defi_trade.transfer``, with the same contract: ``dry_run``
defaults TRUE, ``max_spend_usd`` is asserted against the SIMULATED outflow, the
guard verdict and the lane are shown, a refusal says "NOT SENT — nothing was
broadcast", the owner-queue lane does not execute, ``gate.reserve()`` spans
check -> broadcast -> record, the ledger records on broadcast (a failed landing
still paid its fee), and the receipt states stay distinct.

There is no EVM transaction here, so ``tx_guard.authorize`` cannot run. The
steps are the ones ``solana_swap`` mirrors, through the SAME seams — the turn
gate (``DefiTradeTool._solana_turn_gate``: principal, kill-switch, entry pause,
forged turns, the goal lane), the vetted simulation
(``core.wallet.solana_tx_inspect.simulate``: program allowlist, authority
taxonomy, measured fee), PolicyGate, and ``tx_guard``'s autonomous ceiling.

What is asserted from the simulation, and why it can be EXACT here:

* **native** — one System ``Transfer``. The measured SOL leaving, less the
  measured fee, must equal the declared amount to the lamport; no token may
  move and no authority may change.
* **SPL** — ``CreateIdempotent`` for the recipient's associated account, then
  ``TransferChecked``. Our token delta must be exactly ``-amount`` of the mint
  and nothing else; the SOL beyond the fee is the recipient account's RENT
  (zero when it already exists), bounded by :data:`MAX_ATA_RENT_LAMPORTS` and
  CHARGED to the caps. The only authority change allowed is the one the
  delta parser reports for an account we funded that ends up owned by the
  recipient — which is exactly what creating their account is.

⚠️ Token-2022 mints can carry extensions that make "sent N, they received N"
false (a transfer fee skims the recipient's side, which our deltas do not see)
or make the transfer do something else (a transfer hook runs arbitrary code).
Only extensions this verb can reason about pass; anything unknown refuses.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: A recipient associated token account's rent, bounded. Classic SPL: 165 bytes,
#: 2,039,280 lamports on mainnet. Token-2022 accounts carry the ImmutableOwner
#: extension (plus TransferFeeAmount on a fee mint), a few bytes more. Headroom
#: is ~1.5x so a rent-parameter rise does not become an unexplained refusal,
#: while anything materially larger is not the account we asked to create.
MAX_ATA_RENT_LAMPORTS = 3_000_000

_NOT_SENT = "RESULT: NOT SENT — nothing was broadcast."

#: Token-2022 extensions that do not change what a plain TransferChecked does
#: to the sender or the recipient.
_BENIGN_EXTENSIONS = frozenset({
    "metadataPointer", "tokenMetadata", "groupPointer", "groupMemberPointer",
    "tokenGroup", "tokenGroupMember", "mintCloseAuthority", "permanentDelegate",
    "confidentialTransferMint",
})


class SolanaTransferParams(BaseModel):
    token: str = Field(..., description=(
        "'native' (or 'SOL') to send SOL itself, or the SPL token's MINT "
        "address (base58). A ticker is not accepted — resolve it to a mint "
        "with defi_data.token_resolve first. Wrapped SOL: send 'native'."))
    to: str = Field(..., description=(
        "Recipient WALLET address (base58). Not a token account: the "
        "recipient's token account is derived and created if missing. Solana "
        "addresses have no checksum — a mistyped one is a valid different "
        "account, so copy it exactly."))
    amount: float = Field(..., gt=0, description=(
        "Human amount to send (e.g. 0.25 SOL, or 10 of the token)."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD you authorize for this send, INCLUDING the fee and any "
        "rent for creating the recipient's token account. Asserted against "
        "the SIMULATED outflow."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) builds, simulates and returns the guard's verdict "
        "without broadcasting. Set false to actually move funds."))


# -- seams (monkeypatched in tests) ------------------------------------------

def _rpc(method: str, params: list):
    from core.wallet import solana_onchain
    return solana_onchain._rpc(method, params)


def _recent_blockhash() -> str:
    from core.wallet.solana_rail import SolanaRail
    return SolanaRail(signer=None).recent_blockhash()


# -- pure helpers ------------------------------------------------------------

def _to_raw(amount: float, decimals: int) -> int:
    """Human -> raw units, EXACTLY; refuses precision the token does not have."""
    try:
        scaled = Decimal(str(amount)) * (Decimal(10) ** int(decimals))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{amount!r} is not a usable amount ({exc})")
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"{amount!r} has more precision than the token's {decimals} decimals")
    raw = int(scaled)
    if raw <= 0 or raw >= 2 ** 64:
        raise ValueError(f"{amount!r} is {raw} raw units, outside a u64")
    return raw


def extension_refusal(extensions) -> Optional[str]:
    """Why a Token-2022 mint's extensions make a plain send unassertable."""
    for name, state in extensions or ():
        state = state or {}
        if name in _BENIGN_EXTENSIONS:
            continue
        if name == "transferFeeConfig":
            bps = [int(((state.get(k) or {}).get("transferFeeBasisPoints") or 0))
                   for k in ("olderTransferFee", "newerTransferFee")]
            if any(bps):
                return (f"the mint carries a transferFeeConfig extension "
                        f"({max(bps)} bps) — the recipient would receive less "
                        f"than was sent, and our simulation cannot see their side")
            continue
        if name == "transferHook":
            if state.get("programId"):
                return (f"the mint carries a transferHook extension — every "
                        f"transfer runs program {state.get('programId')}, whose "
                        f"effects this verb cannot assert")
            continue
        if name == "defaultAccountState":
            if str(state.get("accountState") or "").lower() == "frozen":
                return ("the mint's defaultAccountState extension creates "
                        "recipient accounts FROZEN — the recipient could not "
                        "move what was sent")
            continue
        if name == "pausableConfig":
            if state.get("paused"):
                return "the mint's pausableConfig extension says transfers are PAUSED"
            continue
        return (f"the mint carries the Token-2022 extension {name!r}, which this "
                f"verb cannot reason about — refusing rather than assuming it "
                f"is harmless")
    return None


def _idem(mint: Optional[str], to: str, amount_raw: int, execution_context) -> str:
    from tools.defi.trade_tool import _turn_id
    material = "|".join(("solana_transfer", mint or "native", to, str(amount_raw),
                         _turn_id(execution_context)))
    return "defi_solana_transfer:" + hashlib.sha256(material.encode()).hexdigest()[:32]


# -- the verb ----------------------------------------------------------------

async def perform_solana_transfer(tool, params, execution_context=None):
    from core.wallet import spl_token, tx_guard, tx_notify
    from core.wallet.addresses import normalize_for_chain
    from core.wallet.solana_rail import confirmation_outcome
    from tools.defi.bridge_verb import _solana_turn_refusal
    from tools.defi.trade_tool import _WSOL_MINT, _solana_trade_enabled

    def refuse(why: str):
        return tool._ar(error=f"refused: {why}. {_NOT_SENT}")

    if not _solana_trade_enabled():
        from core.remedy import flag_remedy
        return refuse("the Solana money rail is off (SOLANA_TRADE_ENABLED) — "
                      + flag_remedy("SOLANA_TRADE_ENABLED"))

    wallet = tool._get_wallet()
    if wallet is None:
        return refuse("agent wallet not enabled (AGENT_WALLET_ENABLED)")
    try:
        signer = wallet.solana_signer()
    except Exception as exc:
        return refuse(f"the Solana signer is unavailable ({exc})")
    if signer is None:
        return refuse("no Solana signer — this needs the 'solana' extra "
                      "(pip install 'polyrob[solana]')")
    gate = getattr(wallet, "policy", None)
    if gate is None:
        return refuse("the wallet exposes no PolicyGate — a money verb may not "
                      "run ungoverned")
    me = signer.address

    token = str(params.token or "").strip()
    native = token.lower() in ("native", "sol")
    try:
        to = normalize_for_chain("solana", params.to)
        mint = None if native else normalize_for_chain("solana", token)
        _check_pubkey(to)
        if mint:
            _check_pubkey(mint)
    except ValueError as exc:
        return refuse(str(exc))
    if mint == _WSOL_MINT:
        return refuse("that is the wrapped-SOL mint — to send SOL pass "
                      "token='native'")
    if to == me:
        return refuse("the recipient is this wallet's own address — sending to "
                      "yourself moves nothing but the fee")
    if mint and to == mint:
        return refuse("the recipient is the token's mint, not a wallet")

    # The SAME turn gate solana_swap and the Solana-origin bridge run: principal,
    # owner kill-switch, entry pause (a send is never exit-shaped), forged turns,
    # and the DEFI_AUTONOMOUS_TURN_TRADING goal lane.
    turn_err, autonomous_origin = _solana_turn_refusal(tool, execution_context)
    if turn_err:
        return tool._ar(error=f"{turn_err} {_NOT_SENT}")

    # The recipient must be a wallet. A token account or a mint "owns" nothing
    # an associated account could be derived for, and SOL sent to a program is
    # usually gone. An unreadable recipient is unknown, not a wallet.
    try:
        rec = await asyncio.to_thread(_rpc, "getAccountInfo",
                                      [to, {"encoding": "jsonParsed"}])
    except Exception as exc:
        return refuse(f"could not read the recipient account {to} ({exc})")
    rec_value = (rec or {}).get("value") if isinstance(rec, dict) else None
    recipient_exists = rec_value is not None
    if isinstance(rec_value, dict):
        from core.wallet.solana_simulation import SPL_TOKEN_PROGRAMS
        if str(rec_value.get("owner") or "") in SPL_TOKEN_PROGRAMS:
            return refuse(f"{to} is a token account or a mint, not a wallet "
                          f"address — send to the recipient's WALLET address")
        if rec_value.get("executable"):
            return refuse(f"{to} is a program, not a wallet")

    if native:
        decimals, program, shown = 9, None, "SOL"
    else:
        try:
            info = await asyncio.to_thread(_rpc, "getAccountInfo",
                                           [mint, {"encoding": "jsonParsed"}])
        except Exception as exc:
            return refuse(f"could not read the mint {mint} ({exc}), so its "
                          f"decimals are unknown")
        state = spl_token.decode_mint_account(info)
        if state is None or state.get("decimals") is None:
            return refuse(f"{mint} does not read as an SPL mint with decimals — "
                          f"refusing to size a send in an unknown denomination")
        if state.get("type") not in (None, "mint"):
            return refuse(f"{mint} is a token {state.get('type')}, not a mint")
        why = extension_refusal(state.get("extensions"))
        if why:
            return refuse(why)
        decimals, program = int(state["decimals"]), state["program"]
        shown = f"of mint {mint}"

    try:
        amount_raw = _to_raw(params.amount, decimals)
    except ValueError as exc:
        return refuse(str(exc))

    # What the wallet HOLDS. None is unknown — never zero, never enough.
    if native:
        try:
            bal = await asyncio.to_thread(_rpc, "getBalance", [me])
            held = int((bal or {}).get("value")) if isinstance(bal, dict) else None
        except Exception:
            held = None
    else:
        # The account the transfer debits — our associated account under the
        # mint's OWN program. (`solana_onchain.token_balances` scans the classic
        # program only, so a Token-2022 holding would read as zero there.)
        held = await asyncio.to_thread(
            _source_balance, spl_token.associated_token_address(me, mint, program),
            owner=me, mint=mint)
    if held is None:
        return refuse("the wallet's balance could not be read, so this send "
                      "cannot be shown to be covered")
    if held < amount_raw:
        return refuse(f"the wallet holds {held} raw units, less than the "
                      f"{amount_raw} to send")

    try:
        blockhash = await asyncio.to_thread(_recent_blockhash)
    except Exception as exc:
        return refuse(f"could not read a recent blockhash ({exc})")
    try:
        tx, dest_account = spl_token.build_send(
            payer=me, to=to, amount_raw=amount_raw, recent_blockhash=blockhash,
            mint=mint, decimals=decimals, token_program=program)
    except spl_token.SplBuildError as exc:
        return refuse(str(exc))
    raw_tx = bytes(tx)
    if spl_token.signer_pubkeys(tx) != [me]:
        return refuse("the built transaction needs a signer other than this wallet")

    try:
        deltas = await asyncio.to_thread(
            lambda: tool._solana_simulate(raw_tx=raw_tx, owner=me,
                                          mints=((mint,) if mint else ())))
    except Exception as exc:
        return refuse(f"the simulation raised ({exc})")
    if deltas is None or not deltas.ok:
        reason = getattr(deltas, "reason", "no result") if deltas else "no result"
        hint = ("; a brand-new address must receive at least the rent-exempt "
                "minimum (~0.00089 SOL)" if native and not recipient_exists else "")
        return refuse(f"the simulation did not pass ({reason}){hint}. A "
                      f"simulation that did not run is not one that passed")

    header = (f"solana transfer {params.amount:g} {shown} -> {to}\n")
    check = _assert_deltas(deltas, native=native, mint=mint, to=to,
                           amount_raw=amount_raw)
    if isinstance(check, str):
        return tool._ar(content=header + f"  guard: refused — {check}\n  {_NOT_SENT}")
    lamports_out, rent = check
    header += (f"  simulated: {lamports_out} lamports leave (fee "
               f"{deltas.fee_lamports}"
               + ("" if native else
                  (f", {rent} rent — creates the recipient's token account "
                   f"{dest_account}" if rent else
                   f"; the recipient's token account {dest_account} already exists"))
               + ")" + ("" if native else f"; token delta -{amount_raw} raw") + "\n")

    # Valuation: the measured SOL leaving (principal, fee, rent) at the pinned
    # wSOL price, plus the token at a trustworthy price (USDC at $1.00 by
    # definition). Unpriced refuses: no cap can bound a number we do not have,
    # and booking it at $0.00 would widen every other verb's headroom.
    try:
        sol_px = tool._price("solana", _WSOL_MINT)
    except Exception:
        sol_px = None
    if not sol_px or sol_px <= 0:
        return tool._ar(content=header + (
            "  guard: refused — SOL could not be priced, so the outflow cannot "
            f"be held to any cap\n  {_NOT_SENT}"))
    amount_usd = lamports_out / 1e9 * float(sol_px)
    if not native:
        if mint == tool._solana_usdc_mint():
            token_px = 1.0
        else:
            try:
                token_px = tool._price("solana", mint)
            except Exception:
                token_px = None
        if not token_px or token_px <= 0:
            return tool._ar(content=header + (
                f"  guard: refused — the token {mint} could not be priced by a "
                f"trustworthy source, so the send cannot be held to any cap\n"
                f"  {_NOT_SENT}"))
        amount_usd += amount_raw / (10 ** decimals) * float(token_px)
    amount_usd = round(amount_usd, 4)
    declared = float(params.max_spend_usd)
    header += f"  simulated value: ${amount_usd:.4f}\n"
    if amount_usd > declared:
        return tool._ar(content=header + (
            f"  guard: refused — ${amount_usd:.4f} exceeds the declared "
            f"max_spend_usd ${declared:.4f}\n  {_NOT_SENT}"))

    idem = _idem(mint, to, amount_raw, execution_context)
    label = f"{params.amount:g} {'SOL' if native else mint[:8] + '…'}"
    async with gate.reserve():
        # tx_guard step 0: a genuine owner turn is not bound by the pause or the
        # autonomous ceiling; an owner grant for this call clears the ceiling.
        from core.money.ledger import pause_probe
        from tools.controller.turn_origin import _is_forged_or_autonomous_turn
        owner_direct, owner_granted = tx_guard.owner_authority(
            execution_context, _is_forged_or_autonomous_turn, tool, params)
        with pause_probe((lambda: False) if owner_direct else tx_guard._halted):
            verdict = gate.check(venue="defi", amount_usd=amount_usd,
                                 idempotency_key=idem)
        if not verdict.allowed:
            return tool._ar(content=header + (
                f"  guard: refused by PolicyGate: {verdict.reason}\n  {_NOT_SENT}"))
        ceiling = tx_guard.autonomous_max_usd(*tx_guard.ceiling_scope(execution_context))
        if amount_usd > ceiling and not owner_direct and not owner_granted:
            return tool._ar(content=header + (
                f"  guard: owner approval required: ${amount_usd:.2f} is above "
                f"the autonomous ceiling ${ceiling:.2f}\n  lane:  owner_queue\n"
                f"  {_NOT_SENT}"))
        if autonomous_origin and not getattr(gate, "has_daily_cap", False):
            return tool._ar(content=header + (
                "  guard: refused — unattended sending needs an aggregate damage "
                "bound; set WALLET_DAILY_CAP_USD\n  lane:  owner_queue\n"
                f"  {_NOT_SENT}"))
        _lane = ("owner_direct" if owner_direct
                 else "owner_approved" if owner_granted else "autonomous")
        header += (f"  guard: allowed — simulated deltas match the declared send, "
                   f"within max_spend_usd and the caps\n"
                   f"  lane:  {_lane}\n")
        if params.dry_run:
            return tool._ar(content=header + (
                "  RESULT: DRY RUN — the guard would allow this, but nothing was "
                "broadcast. Re-run with dry_run=false to send."))

        # RPC trust, the solana_swap mirror: the simulation, the deltas and the
        # caps all read from the RPC. Dry runs above stay available unpinned.
        if not os.getenv("DEFI_SOLANA_RPC", "").strip():
            return tool._ar(content=header + (
                "  guard: refused — no pinned RPC for solana. The simulation, "
                "the deltas and the caps all read from it, so the shared public "
                f"endpoint cannot authorize moving funds — set DEFI_SOLANA_RPC\n"
                f"  {_NOT_SENT}"))
        try:
            bh_ok, bh_detail = await asyncio.to_thread(
                tool._solana_blockhash_valid, raw_tx)
        except Exception as exc:
            bh_ok, bh_detail = False, f"check failed: {exc}"
        if not bh_ok:
            return tool._ar(content=header + (
                f"  guard: refused — the blockhash is no longer valid on the "
                f"pinned RPC ({bh_detail}); re-run to rebuild\n  {_NOT_SENT}"))

        try:
            signature = await asyncio.to_thread(tool._solana_send, raw_tx, signer)
        except Exception as exc:
            # The RPC may have accepted the bytes and lost its reply: that is
            # UNKNOWN, never "not sent". The submission journal holds the
            # signature and blocks a second spend until it is reconciled.
            from core.wallet.broadcast.evm import broadcast_failure_text
            return tool._ar(error=broadcast_failure_text(
                exc, nothing="funds were NOT sent"))
        # Recorded on broadcast: a send that lands and FAILS still paid its fee,
        # and one that lands late is a spend the caps must already see.
        if _lane != "autonomous" and hasattr(gate, "note_lane"):
            gate.note_lane(idem, _lane)
        gate.record(venue="defi", action="solana_transfer", amount_usd=amount_usd,
                    counterparty=to, idempotency_key=idem, result_ref=signature,
                    chain="solana")
        _used, _limit = tx_notify.caps_from_gate(gate)
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="solana_transfer", route="solana", chain="solana",
            amount_in=label, usd=amount_usd, tx_ref=signature, lane=_lane,
            cap_used_usd=_used, cap_limit_usd=_limit), settled=False)

    # Outside the reservation: polling can take a minute and the spend is
    # already recorded. Off the event loop: the rail sleeps between polls.
    try:
        ok, detail = await asyncio.to_thread(tool._solana_confirm, signature)
    except Exception as exc:
        ok, detail = False, f"status read failed: {exc}"
    outcome = confirmation_outcome(ok, detail)
    tool._notify_tx(execution_context, tx_notify.TxNotice(
        verb="solana_transfer", route="solana", chain="solana", amount_in=label,
        usd=amount_usd, tx_ref=signature,
        state={"confirmed": tx_notify.STATE_CONFIRMED,
               "reverted": tx_notify.STATE_REVERTED}.get(
            outcome, tx_notify.STATE_IN_FLIGHT),
        detail=f"to {to}; {detail}", ledger_recorded=True), settled=True)

    if outcome == "confirmed":
        return tool._ar(content=header + (
            f"  RESULT: SENT AND CONFIRMED ({detail})\n  sig: {signature}"))
    if outcome == "reverted":
        return tool._ar(content=header + (
            f"  RESULT: FAILED ON-CHAIN — the transfer did NOT happen, but the "
            f"fee was spent.\n  sig: {signature}\n  detail: {detail}"))
    return tool._ar(content=header + (
        f"  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. It may still "
        f"land, or the blockhash may have expired — do NOT retry blindly; check "
        f"the signature first.\n  sig: {signature}\n  detail: {detail}"))


def _source_balance(account: str, *, owner: str, mint: str) -> Optional[int]:
    """Raw balance of our token *account*: 0 when it does not exist, None when
    it cannot be read or is not an unfrozen *mint* account owned by *owner*."""
    try:
        res = _rpc("getAccountInfo", [account, {"encoding": "jsonParsed"}])
    except Exception:
        return None
    if not isinstance(res, dict):
        return None
    value = res.get("value")
    if value is None:
        return 0
    try:
        info = value["data"]["parsed"]["info"]
        if info.get("mint") != mint or info.get("owner") != owner:
            return None
        if str(info.get("state") or "") == "frozen":
            return None
        return int(info["tokenAmount"]["amount"])
    except (KeyError, TypeError, ValueError):
        return None


def _check_pubkey(addr: str) -> None:
    """A Solana account key is exactly 32 bytes of base58."""
    from solders.pubkey import Pubkey
    try:
        Pubkey.from_string(addr)
    except Exception:
        raise ValueError(f"{addr!r} is not a 32-byte base58 Solana address")


def _assert_deltas(deltas, *, native: bool, mint: Optional[str], to: str,
                   amount_raw: int):
    """``(lamports_out, rent)`` when the simulation shows exactly the declared
    send, else the refusal sentence."""
    fee = getattr(deltas, "fee_lamports", None)
    if fee is None:
        return ("the simulation did not compute the fee, so SOL leaving cannot "
                "be told apart from the fee")
    lamports_out = -int(deltas.native_delta or 0)
    retained = int(getattr(deltas, "retained_rent_lamports", 0) or 0)
    beyond_fee = lamports_out - int(fee) - retained
    moved = {m: d for m, d in (deltas.token_deltas or {}).items() if d}
    grants = tuple(deltas.authority_grants or ())

    if native:
        if moved:
            return f"a SOL send moved tokens too ({moved})"
        if grants:
            return f"the transaction changes authority over our accounts ({list(grants)})"
        if lamports_out <= 0:
            return ("the simulation measured no SOL leaving — an unobserved "
                    "outflow is a measurement failure, not a free transfer")
        if beyond_fee != amount_raw:
            return (f"{beyond_fee} lamports leave beyond the fee, but "
                    f"{amount_raw} was declared")
        return lamports_out, 0

    allowed = {("owner_changed", mint, to)}
    unexpected = [g for g in grants if tuple(g) not in allowed]
    if unexpected:
        return f"the transaction changes authority over our accounts ({unexpected})"
    if mint not in moved:
        return (f"the simulation did not observe the token {mint} leaving — a "
                f"check that did not run is not a check that passed")
    if moved != {mint: -amount_raw}:
        return (f"the simulated token deltas are {moved}, not exactly "
                f"-{amount_raw} of {mint}")
    if beyond_fee < 0:
        return f"the simulation shows SOL arriving ({-beyond_fee} lamports)"
    if beyond_fee > MAX_ATA_RENT_LAMPORTS:
        return (f"{beyond_fee} lamports leave beyond the fee — more than one "
                f"token account's rent ({MAX_ATA_RENT_LAMPORTS}) explains")
    return lamports_out, beyond_fee
