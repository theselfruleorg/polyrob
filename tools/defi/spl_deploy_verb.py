"""``solana_deploy_token`` — a fixed-supply SPL token (042b).

The Solana twin of ``deploy_token``, and the assertions are shaped differently
because the chain is. On EVM the proof is the produced RUNTIME BYTECODE, which
the guard compares byte for byte against a pinned template. A mint has no
bytecode: it is 82 bytes of state owned by a program everyone shares. So what
is proven here is the STATE, in two places:

* **before broadcast** — the transaction's program set is the base allowlist and
  nothing else, exactly two signers (the payer and the mint creating itself),
  the simulated SOL outflow is plausible rent, and the simulation measures the
  WHOLE declared supply arriving in our own token account;
* **after confirmation** — ``getAccountInfo(mint)`` shows ``mintAuthority: null``,
  ``freezeAuthority: null`` and the exact supply.

⚠️ That second read is not belt-and-braces, it is the only place the revocation
can be seen. ``solana_simulation.parse_deltas``' authority taxonomy reads token
ACCOUNT fields (owner, delegate, closeAuthority, state) and is structurally
blind to ``SetAuthority`` on a MINT — so "the supply is fixed" is a claim the
deltas cannot make, and inferring it from them would be exactly the kind of
confident-and-wrong report this tree keeps learning not to write.

The name and symbol are ON-CHAIN, in the mint itself — Token-2022's
``MetadataPointer`` + ``TokenMetadata`` extensions, no Metaplex account. The
metadata UPDATE authority is revoked in the same transaction as the mint
authority: a token whose supply is fixed but whose name can be swapped later is
only half immutable, and the half that moves is the half a buyer reads.

⚠️ A few older AMMs do not support Token-2022. Jupiter (our own Solana route)
and the major venues do.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: A brand-new mint (with metadata) plus a token account, MEASURED against
#: mainnet on 2026-09-13 by simulating the whole flow: the payer's balance moved
#: 4,007,960 lamports (~0.004 SOL). The metadata extensions are most of the
#: difference from a bare classic mint's 2,565,240. The guard's own `MAX_PLAUSIBLE_RENT_LAMPORTS` is the
#: outer bound (0.01 SOL); this is the tighter, shape-specific one — anything
#: materially above it is not the transaction that was built.
#:
#: Headroom is deliberate and generous (roughly 2x) because rent is a consensus
#: parameter that HAS moved: the widely-quoted figure would have made this
#: 3,510,880, and hard-fitting the measured number would turn a parameter rise
#: into an unexplained refusal.
MAX_EXPECTED_LAMPORTS = 8_000_000


def _refuse_shape(tx, *, payer: str, mint: str) -> Optional[str]:
    """The transaction must be the one we just built. None when it is."""
    from core.wallet import spl_token

    signers = spl_token.signer_pubkeys(tx)
    if len(signers) != 2:
        return (f"refused: a mint creation needs exactly two signatures (the "
                f"payer and the new mint), this one needs {len(signers)}")
    if signers[0] != payer:
        return (f"refused: the fee payer is {signers[0]}, not this wallet")
    if signers[1] != mint:
        return (f"refused: the second signer is {signers[1]}, not the mint "
                f"account being created")
    return None


async def perform_solana_deploy_token(tool, params, execution_context=None):
    from core.wallet import solana_onchain, spl_token
    from core.wallet.solana_rail import SolanaRail, confirmation_outcome
    from core.wallet.authority import leaf_refusal, spend_pause_refusal
    from tools.defi.deploy_verb import FLAG, deploy_enabled

    if not deploy_enabled():
        return tool._ar(error=(
            f"contract deployment is off — set {FLAG}=true to arm it. Nothing "
            f"was broadcast."))
    from tools.defi.trade_tool import _solana_trade_enabled
    if not _solana_trade_enabled():
        return tool._ar(error=(
            "the Solana money rail is off — set SOLANA_TRADE_ENABLED=true. "
            "Nothing was broadcast."))

    turn_err = leaf_refusal(execution_context, "deploy a token")
    if turn_err:
        return tool._ar(error=turn_err)
    if not params.dry_run:
        paused = spend_pause_refusal()
        if paused:
            return tool._ar(error=paused + " RESULT: NOT SENT.")

    try:
        supply_raw = _supply_to_raw(params.supply, params.decimals)
    except ValueError as exc:
        return tool._ar(error=f"refused: {exc}")

    wallet = tool._get_wallet()
    if wallet is None:
        return tool._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
    try:
        signer = wallet.solana_signer()
    except Exception as exc:
        return tool._ar(error=f"the Solana signer is unavailable: {exc}")
    if signer is None:
        return tool._ar(error=(
            "no Solana signer — this needs the 'solana' extra "
            "(pip install 'polyrob[solana]')"))
    payer = signer.address
    gate = wallet.policy

    rail = SolanaRail(signer=None)
    try:
        blockhash = rail.recent_blockhash()
    except Exception as exc:
        return tool._ar(error=f"could not read a recent blockhash: {exc}")

    # Fund for the mint AND the metadata it is about to grow: TokenMetadata
    # reallocs, and a short account fails THERE rather than at the instruction
    # that under-funded it.
    name = (params.name or params.symbol).strip()
    uri = (params.uri or "").strip()
    space = spl_token.MINT_WITH_POINTER_LEN + spl_token.metadata_space(
        name=name, symbol=params.symbol, uri=uri)
    mint_rent = spl_token.rent_exempt_from_rpc(
        space, lambda m, p: solana_onchain._rpc(m, p))
    try:
        tx, mint_kp, mint, ata = spl_token.build_fixed_supply_mint(
            payer=payer, decimals=params.decimals, supply_raw=supply_raw,
            recent_blockhash=blockhash, mint_rent=mint_rent,
            name=name, symbol=params.symbol, uri=uri)
    except spl_token.SplBuildError as exc:
        return tool._ar(error=f"refused: {exc}")

    shape_err = _refuse_shape(tx, payer=payer, mint=mint)
    if shape_err:
        return tool._ar(error=shape_err)

    # Simulate with mints=() on purpose: `atas_for_mint` spends TWO of the five
    # observable-account slots deriving speculative ATAs under both token
    # programs for a mint that does not exist yet, and one of them never will.
    # The tx's own account keys carry the mint and the real ATA anyway.
    try:
        deltas = tool._solana_simulate(raw_tx=bytes(tx), owner=payer, mints=())
    except Exception as exc:
        return tool._ar(error=f"refused: simulation raised ({exc})")
    if not deltas.ok:
        return tool._ar(error=(
            f"refused: simulation not trustworthy — {deltas.error}"))

    refusal = _assert_deltas(deltas, mint=mint, supply_raw=supply_raw)
    if refusal:
        return tool._ar(error=refusal)

    lamports_out = -int(deltas.native_delta)
    sol_out = lamports_out / 1_000_000_000
    amount_usd = _price_sol(tool, sol_out)

    header = (
        f"deploy SPL token on solana\n"
        f"  mint:     {mint}\n"
        f"  supply:   {params.supply:g} whole units ({supply_raw} raw at "
        f"{params.decimals} decimals), all to {ata}\n"
        f"  fixed:    the mint authority is REVOKED in the same transaction, "
        f"and no freeze authority is ever created\n"
        f"  metadata: ON-CHAIN via Token-2022 — name {name!r}, symbol "
        f"{params.symbol!r}"
        + (f", uri {uri}" if uri else ", no uri")
        + ". The update authority is revoked in the same transaction, so the "
          "name cannot be swapped later either.\n"
        f"  cost:     {sol_out:.9f} SOL rent + fees"
        f"{'' if amount_usd is None else f' (${amount_usd:.4f})'}\n")

    if amount_usd is None:
        return tool._ar(error=header + (
            "  RESULT: NOT SENT — SOL could not be priced, and an unpriced "
            "outflow booked at $0.00 would widen every other cap by its own "
            "size."))
    if amount_usd > params.max_spend_usd:
        return tool._ar(content=header + (
            f"  RESULT: NOT SENT — ${amount_usd:.4f} exceeds the declared "
            f"max_spend_usd ${params.max_spend_usd:.4f}."))

    idem = f"solana_deploy_token:{mint}"
    async with gate.reserve():
        verdict = gate.check(venue="defi", amount_usd=amount_usd,
                             idempotency_key=idem)
        if not verdict.allowed:
            return tool._ar(content=header + (
                f"  RESULT: NOT SENT — refused by PolicyGate: {verdict.reason}"))

        if params.dry_run:
            return tool._ar(content=header + (
                "  RESULT: DRY RUN — every assertion passed and nothing was "
                "broadcast. Re-run with dry_run=false to deploy."))

        # The SAME check `solana_swap` makes, by the same means (the env var,
        # not a guess at the URL shape) so the two cannot drift into different
        # ideas of what "pinned" is.
        import os as _os
        if not _os.getenv("DEFI_SOLANA_RPC", "").strip():
            return tool._ar(content=header + (
                "  RESULT: NOT SENT — refused: no pinned RPC for solana. The "
                "simulation, the deltas and the caps all read from it, so the "
                "shared public endpoint cannot be the trust anchor for moving "
                "funds — set DEFI_SOLANA_RPC. Dry runs are unaffected."))

        try:
            signed = signer.sign_transaction_with(tx, [mint_kp])
        except Exception as exc:
            return tool._ar(error=f"refused at signing: {exc} — nothing was broadcast")
        try:
            signature = SolanaRail(signer=signer).send_raw(bytes(signed))
        except Exception as exc:
            return tool._ar(error=f"broadcast failed: {exc} — nothing was deployed")

        gate.record(venue="defi", action="solana_deploy_token",
                    amount_usd=amount_usd, counterparty=mint,
                    idempotency_key=idem, result_ref=signature,
                    chain="solana")

    import asyncio
    ok, detail = await asyncio.to_thread(tool._solana_confirm, signature)
    outcome = confirmation_outcome(ok, detail)
    if outcome != "confirmed":
        return tool._ar(content=header + (
            f"  RESULT: {'REVERTED' if outcome == 'reverted' else 'UNCONFIRMED'} "
            f"— {detail}\n  signature: {signature}\n"
            f"  ⚠️ do NOT retry blindly: a second run creates a SECOND mint."))

    proof = _prove_fixed_supply(mint, supply_raw, params.decimals)
    return tool._ar(content=header + proof + (
        f"  RESULT: DEPLOYED AND CONFIRMED\n"
        f"  mint: {mint}\n  signature: {signature}"))


# ==========================================================================
# Assertions
# ==========================================================================

def _assert_deltas(deltas, *, mint: str, supply_raw: int) -> Optional[str]:
    """What the SIMULATION must show. None when it all holds."""
    from core.wallet.solana_simulation import is_plausible_rent

    if deltas.grants_authority():
        return (f"refused: the transaction grants an authority over one of our "
                f"token accounts ({deltas.authority_grants}) — a mint creation "
                f"grants nothing")

    moved = int(deltas.native_delta)
    if moved > 0:
        return (f"refused: the simulation shows SOL arriving ({moved} lamports). "
                f"A mint creation only pays rent")
    if moved == 0:
        return ("refused: the simulation measured NO SOL movement, but creating "
                "a mint and a token account costs rent — treating that as a "
                "measurement failure, not as a free transaction")
    if not is_plausible_rent(moved):
        return (f"refused: the transaction moves {-moved} lamports, more than "
                f"rent for two accounts could be")
    if -moved > MAX_EXPECTED_LAMPORTS:
        return (f"refused: {-moved} lamports is above the "
                f"{MAX_EXPECTED_LAMPORTS} expected for a mint plus a token "
                f"account — this is not the transaction that was built")

    received = (deltas.token_deltas or {}).get(mint)
    if received is None:
        # The ATA is created by this transaction, so `parse_deltas` records its
        # whole balance as an inflow. Not seeing it means the account fell
        # outside the observable set, and an unobserved mint is an unproven one.
        return ("refused: the simulation did not measure the new token arriving "
                "in our account. The supply cannot be confirmed, so this is a "
                "measurement failure rather than a successful mint")
    if received != supply_raw:
        return (f"refused: the simulation mints {received} raw units, not the "
                f"declared {supply_raw}")
    return None


def _prove_fixed_supply(mint: str, supply_raw: int, decimals: int) -> str:
    """Read the mint back and state what is actually true of it.

    ⚠️ This is the ONLY place the revocation can be observed: the delta parser's
    authority taxonomy reads token-ACCOUNT fields and cannot see `SetAuthority`
    on a mint. An unreadable mint renders UNVERIFIED — never "fixed supply".
    """
    from core.wallet import solana_onchain, spl_token
    try:
        info = solana_onchain._rpc(
            "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    except Exception as exc:
        logger.debug("solana deploy: mint read-back failed (%s)", exc)
        return ("  ⚠️ VERIFICATION UNAVAILABLE — the mint could not be read back, "
                "so 'fixed supply' is unverified. Check the explorer before "
                "telling anyone the supply cannot change.\n")
    state = spl_token.decode_mint_account(info)
    if state is None:
        return ("  ⚠️ VERIFICATION UNAVAILABLE — the mint did not decode as an "
                "SPL mint account.\n")

    problems = []
    if state.get("mint_authority") is not None:
        problems.append(f"mint authority is STILL {state['mint_authority']}")
    if state.get("freeze_authority") is not None:
        problems.append(f"freeze authority is {state['freeze_authority']}")
    if not state.get("has_metadata"):
        problems.append("the mint carries NO on-chain metadata")
    if state.get("update_authority") is not None:
        problems.append(
            f"the metadata update authority is STILL "
            f"{state['update_authority']} — the name can be changed")
    if int(state.get("supply") or 0) != supply_raw:
        problems.append(f"supply reads {state.get('supply')}, not {supply_raw}")
    if int(state.get("decimals") if state.get("decimals") is not None else -1) != decimals:
        problems.append(f"decimals read {state.get('decimals')}, not {decimals}")
    if problems:
        return ("  ⚠️ VERIFIED AND WRONG — " + "; ".join(problems)
                + ". Do NOT describe this token as fixed-supply.\n")
    return (f"  verified:  mint + freeze + metadata authorities all null; "
            f"supply {state['supply']} at {state['decimals']} decimals; "
            f"on-chain name {state.get('name')!r} symbol {state.get('symbol')!r}"
            f" — read back from the mint, not assumed\n")


def _supply_to_raw(supply: float, decimals: int) -> int:
    """Whole units -> raw, EXACTLY, and inside a u64.

    Decimal for the same reason the EVM template uses it: `1e9 * 10**9` in
    binary floating point is not `1e18`, and above 2^53 the error lands in the
    integer part.
    """
    from decimal import Decimal, InvalidOperation

    if supply <= 0:
        raise ValueError("supply must be greater than zero")
    try:
        scaled = Decimal(str(supply)) * (Decimal(10) ** int(decimals))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError(f"supply {supply!r} is not a usable number ({exc})")
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"a supply of {supply:g} is not a whole number of units at "
            f"{decimals} decimals")
    raw = int(scaled)
    if raw >= 2 ** 64:
        raise ValueError(
            f"{supply:g} at {decimals} decimals is {raw} raw units, which does "
            f"not fit in a u64 — Solana supplies are u64. Use fewer decimals or "
            f"a smaller supply.")
    return raw


def _price_sol(tool, sol_out: float) -> Optional[float]:
    """USD of the SOL leaving, or None when it cannot be priced."""
    try:
        from core.wallet import chains
        row = chains.get("solana")
        wsol = getattr(row, "wrapped_native", None) if row else None
        if not wsol:
            return None
        unit = tool._price("solana", wsol)
        return None if unit is None else round(sol_out * float(unit), 2)
    except Exception as exc:
        logger.debug("solana deploy: could not price SOL (%s)", exc)
        return None
