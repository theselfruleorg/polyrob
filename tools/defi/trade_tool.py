"""defi_trade — the agent's on-chain money verbs (proposal 023 T3 + T4).

Verbs: `transfer` (T3), and `approve_token` / `revoke_approval` / `swap` (T4).
`transfer` came first because it is the simplest irreversible action and so the
right thing to prove the rail with; T5 (`contract_write`, arbitrary calls) is
still unbuilt.

Swaps route through the provider seam in `providers/routes/` (proposal 029).
Uniswap V3 (`providers/univ3.py`) is tried FIRST wherever a chain has a verified
deployment, because building the calldata ourselves from pinned addresses is the
strongest trust position available. A third-party aggregator is consulted only
when local construction finds no pool, and only when an operator has named one
(`DEFI_ROUTE_AGGREGATOR`, default off).

That second provider exists because a V3-only rail could not reach the pools
that actually matter: the prod agent screened fresh Base launches for two weeks
and could buy almost none of them, because they pool on Aerodrome or a V2 fork.
What makes a third party's opaque calldata admissible is that `tx_guard` never
reads calldata — it simulates and asserts the observed deltas. See
`providers/routes/__init__.py` for the three properties that must be made
explicit once the route stops being ours.

Which chains these verbs accept is NOT decided here: `core.wallet.chains` is the
one registry, and every verb asks it (`money_ready` for value movement,
`swap_ready` for a route). A chain the registry has not verified is refused by
name — the tool never quietly substitutes a chain that works, because the same
address is a different token on a different chain.

Allowance hygiene is the point of T4: `approve_token` grants an EXACT amount
(unlimited is refused, and the grant is bounded by the caller's declared USD),
and `revoke_approval` sets it back to zero. An approval is a standing claim on
the wallet, so it is never left open by design.

Every path here goes: build → `tx_guard.authorize()` → broadcast → confirm →
record, with `PolicyGate.reserve()` held across the whole span so two concurrent
transfers cannot both clear a nearly-exhausted cap. The tool never decides
policy; it may not broadcast without a `Decision(allowed=True)`.

`dry_run` defaults to TRUE. Moving real funds requires the caller to say so.
"""
from __future__ import annotations  # safe: @BaseTool.action uses explicit param_model

import logging
import time
import types
import uuid
from typing import Optional, Tuple

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool

logger = logging.getLogger(__name__)

_NULL_CONFIG = types.SimpleNamespace()

def _unsupported_chain(chain: str) -> Optional[str]:
    """None when value may move on *chain*, else the reason it may not.

    Delegates to the chain registry, which is the ONE table saying which chains
    are verified. A refusal names the chain the caller asked for and what is
    missing for it — never "use base instead", because silently steering a
    trade to another chain is how the same address becomes a different token.
    """
    from core.wallet import chains
    ok, why = chains.money_capable(chain)
    return None if ok else why


def _no_swap_route(chain: str) -> Optional[str]:
    """None when *chain* has a verified swap route, else the reason."""
    from core.wallet import chains
    ok, why = chains.swap_ready(chain)
    return None if ok else why


def _chain_field_description(verb: str) -> str:
    from core.wallet import chains
    return (f"Chain to {verb} on — CHOOSE IT DELIBERATELY, because the same "
            f"address is a different token on a different chain. Chains that "
            f"can move value: {', '.join(chains.money_chains())}. Gas differs "
            f"by orders of magnitude, so a cent-scale trade can cost more in "
            f"gas on ethereum than it is worth, while base is a fraction of a "
            f"cent. Full guidance:\n{chains.chain_guidance()}")


class TransferParams(BaseModel):
    chain: str = Field("base", description=_chain_field_description("send"))
    token: str = Field(..., description=(
        "CONTRACT ADDRESS of the token to send (0x…). A ticker is not accepted — "
        "resolve it to an address with defi_data.token_resolve first."))
    to: str = Field(..., description="Recipient address (0x…)")
    amount: float = Field(..., gt=0, description="Human amount to send (e.g. 0.25)")
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD you authorize for this transfer. Asserted against the "
        "SIMULATED outflow — if the simulation disagrees, the transfer is refused."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) simulates and returns the guard's verdict without "
        "broadcasting. Set false to actually move funds."))


#: An approval bigger than this is treated as "unlimited" and refused outright.
#: Exact-amount approvals are the whole point of T4's allowance hygiene: an
#: unlimited grant survives the trade and is a standing claim on the wallet.
_UNLIMITED_APPROVAL_FLOOR = 2 ** 128

#: Route-vs-independent-price drift above this is DISAGREES, and a DISAGREES
#: route REFUSES to execute (§1.2, 2026-08-14 — it used to only narrate).
#: The DEFAULT stays 3.0; an operator may widen it with DEFI_ROUTE_DRIFT_MAX_PCT.
_ROUTE_DRIFT_MAX_PCT = 3.0

#: An operator may WIDEN the drift tolerance; nobody may remove it. The check
#: exists because a pool price is a number anyone with capital can seed, and a
#: tolerance wide enough to admit anything is the same as no check at all.
_ROUTE_DRIFT_CEILING_PCT = 25.0


def _route_drift_max_pct() -> float:
    """The drift tolerance, operator-tunable within a hard ceiling.

    3% was calibrated against majors, where an independent price is deep and
    trustworthy. On a fresh memecoin book the independent source is itself thin,
    so ordinary trades routinely imply a price several percent away from it —
    live on prod, a clean NVDAc entry (screen clean, $1.23M liquidity) was
    refused at 10.06% drift. The check was refusing ordinary trades rather than
    manipulated ones at that setting.

    A malformed or non-positive value falls back to the conservative default:
    a typo must never read as "no drift limit".
    """
    import os
    raw = os.getenv("DEFI_ROUTE_DRIFT_MAX_PCT", "").strip()
    if not raw:
        return _ROUTE_DRIFT_MAX_PCT
    try:
        value = float(raw)
    except ValueError:
        return _ROUTE_DRIFT_MAX_PCT
    if value <= 0:
        return _ROUTE_DRIFT_MAX_PCT
    return min(value, _ROUTE_DRIFT_CEILING_PCT)

#: A quote older than this refuses to execute. swap() re-quotes inline today,
#: so this never trips — it exists so a future split of quote and execute
#: (e.g. an owner-approval lane) cannot silently execute a stale price.
_QUOTE_MAX_AGE_SEC = 30.0

#: Wrapped SOL. Jupiter wraps/unwraps native SOL through this mint, so the held
#: balance behind a wSOL sell is the native SOL balance plus any wrapped account.
#: Sourced from the chain registry rather than re-typed: address IS identity
#: here, and two copies of a mint that must agree is a drift waiting to happen.
def _wsol_mint() -> str:
    from core.wallet import chains
    row = chains.get("solana")
    return (row.wrapped_native if row and row.wrapped_native
            else "So11111111111111111111111111111111111111112")


_WSOL_MINT = _wsol_mint()


def _solana_trade_enabled() -> bool:
    """Default OFF. Shipping the Solana rail must change nothing until an
    operator arms it, exactly like DEFI_TRADE_ENABLED for the EVM verbs."""
    import os
    return os.getenv("SOLANA_TRADE_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on")


def _max_slippage_bps() -> int:
    """Default slippage bound (proposal 023 T4: DEFI_MAX_SLIPPAGE_BPS, 100)."""
    from core.env import int_env
    return int_env("DEFI_MAX_SLIPPAGE_BPS", 100)


class ApproveParams(BaseModel):
    chain: str = Field("base", description=_chain_field_description("approve on"))
    token: str = Field(..., description="CONTRACT ADDRESS of the token to approve (0x…)")
    spender: str = Field(..., description="Address being granted the allowance (0x…)")
    amount: float = Field(..., gt=0, description=(
        "EXACT human amount to approve. Unlimited approvals are refused — "
        "approve only what this trade needs, then revoke."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD this approval may put at risk, asserted against the "
        "simulated allowance grant."))
    dry_run: bool = Field(True, description="TRUE simulates only. Set false to sign.")


class RevokeParams(BaseModel):
    chain: str = Field("base", description=_chain_field_description("revoke on"))
    token: str = Field(..., description="CONTRACT ADDRESS of the token (0x…)")
    spender: str = Field(..., description="Address whose allowance is set to ZERO (0x…)")
    dry_run: bool = Field(True, description="TRUE simulates only. Set false to sign.")


class SwapParams(BaseModel):
    chain: str = Field("base", description=_chain_field_description("swap on"))
    token_in: str = Field(..., description=(
        "CONTRACT ADDRESS of the token you are selling (0x…). A ticker is not "
        "accepted — resolve it with defi_data.token_resolve first."))
    token_out: str = Field(..., description="CONTRACT ADDRESS of the token you are buying (0x…)")
    amount_in: float = Field(..., gt=0, description="Human amount of token_in to sell")
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD you authorize to leave the wallet, asserted against the "
        "SIMULATED outflow."))
    slippage_bps: Optional[int] = Field(None, ge=1, le=1000, description=(
        "Max slippage in basis points (100 = 1%). Defaults to "
        "DEFI_MAX_SLIPPAGE_BPS. Bounds amountOutMinimum, so the swap reverts "
        "on-chain rather than filling at a worse price."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) quotes and simulates without broadcasting. Set false "
        "to actually trade."))


class SolanaSwapParams(BaseModel):
    token_in: str = Field(..., description="MINT ADDRESS of the token to sell (base58)")
    token_out: str = Field(..., description="MINT ADDRESS of the token to buy (base58)")
    amount_in: float = Field(..., gt=0, description="Human amount of token_in to sell")
    max_spend_usd: float = Field(..., gt=0, description=(
        "Your declared ceiling for this swap in USD. The guard holds you to it."))
    slippage_bps: Optional[int] = Field(None, ge=1, le=1000, description=(
        "Slippage bound in basis points. Jupiter bakes its own minimum into the "
        "transaction it builds, so this is a bar that minimum must CLEAR — a "
        "looser route is refused, not rewritten."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) simulates and asserts without broadcasting."))


class DefiTradeTool(BaseTool):
    def __init__(self, name: str = "defi_trade", config=None, container=None, *,
                 wallet=None, rail_factory=None, guard_fn=None, price_fn=None,
                 route_fn=None, fallback_price_fn=None, balance_fn=None,
                 solana_quote_fn=None, solana_build_fn=None,
                 solana_simulate_fn=None, solana_send_fn=None,
                 solana_decimals_fn=None, solana_held_fn=None,
                 solana_confirm_fn=None):
        super().__init__(name=name, config=config if config is not None else _NULL_CONFIG,
                         container=container)
        self._wallet = wallet
        self._rail_factory = rail_factory
        self._guard_fn = guard_fn
        self._price_fn = price_fn
        self._route_fn_override = route_fn
        self._fallback_price_fn = fallback_price_fn
        self._balance_fn = balance_fn
        self._solana_quote_fn = solana_quote_fn
        self._solana_build_fn = solana_build_fn
        self._solana_simulate_fn = solana_simulate_fn
        self._solana_send_fn = solana_send_fn
        self._solana_decimals_fn = solana_decimals_fn
        self._solana_held_fn = solana_held_fn
        self._solana_confirm_fn = solana_confirm_fn

    def _ar(self, *, content: str = None, error: str = None):
        from tools.controller.types import ActionResult
        if error is not None:
            return ActionResult(error=error)
        return ActionResult(extracted_content=content)

    def _get_wallet(self):
        if self._wallet is not None:
            return self._wallet
        from core.wallet.factory import get_agent_wallet
        return get_agent_wallet()

    def _route(self, chain, token_in, token_out, amount_in_raw, *, holder,
               slippage_bps):
        """The route seam. Returns ``(RouteQuote | None, reason)``.

        The reason is carried because "no route exists" and "we could not ask"
        are different facts, and the refusal the agent reads must not turn a
        rate-limited provider into a written conclusion that a token is
        unreachable."""
        if self._route_fn_override:
            got = self._route_fn_override(chain, token_in, token_out, amount_in_raw,
                                          holder=holder, slippage_bps=slippage_bps)
            # A test seam may return the bare quote; normalise to (route, why).
            if isinstance(got, tuple):
                return got
            return got, ("" if got is not None else f"no route on {chain}")
        from tools.defi.providers import routes
        return routes.best_route_with_reason(
            chain, token_in, token_out, amount_in_raw, holder=holder,
            slippage_bps=slippage_bps)

    def _price(self, chain, addr):
        if self._price_fn:
            return self._price_fn(chain, addr)
        from tools.defi.providers import dexscreener
        info = dexscreener.token(chain, addr)
        # Only a trustworthy price may bound a cap — a thin, attacker-seedable
        # pool is not a price for this purpose.
        return info.price_usd if info.confidence == "high" else None

    def _fallback_price(self, chain, addr):
        """Best-effort price, NOT confidence-gated (028, 2026-08-22).

        Only ever consulted by the guard's exit exemption, which already
        bounds the grant to the wallet's held balance — the worst case for a
        wrong fallback price is a mispriced (not an unbounded) exit."""
        if self._fallback_price_fn:
            return self._fallback_price_fn(chain, addr)
        from tools.defi.providers import dexscreener
        return dexscreener.token(chain, addr).price_usd

    def _held_balance_raw(self, chain, holder, token):
        if self._balance_fn:
            return self._balance_fn(chain, holder, token)
        from core.wallet.onchain import token_balances
        return token_balances(holder, chain, [token]).get(token)

    @BaseTool.action(
        "Send tokens from the agent wallet to an address. Simulated and asserted "
        "against your declared max_spend_usd before anything is broadcast. "
        "dry_run defaults to TRUE — set it false to actually move funds.",
        param_model=TransferParams)
    async def transfer(self, params: TransferParams, execution_context=None):
        from core.wallet import tx_guard
        from core.wallet.broadcast.evm import EvmRail
        from core.wallet.tokens import get_token_identity, normalize_address

        chain_err = _unsupported_chain(params.chain)
        if chain_err:
            return self._ar(error=chain_err)
        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")

        try:
            token = normalize_address(params.token)
            to = normalize_address(params.to)
        except ValueError as exc:
            return self._ar(error=str(exc))

        ident = get_token_identity(params.chain, token)
        if ident.decimals is None:
            return self._ar(error=(
                f"{token} does not report decimals — refusing to compute an amount "
                f"for a token whose denomination is unknown"))
        amount_raw = int(round(params.amount * (10 ** ident.decimals)))

        signer = wallet.operational_signer()
        gate = wallet.policy
        idem = f"defi_transfer:{params.chain}:{token}:{to}:{amount_raw}:{uuid.uuid4().hex[:8]}"

        intent = tx_guard.TxIntent(
            chain=params.chain, token=token, to=to, amount_raw=amount_raw,
            max_spend_usd=params.max_spend_usd, expected_allowance_grants=(),
            idempotency_key=idem)

        rail = (self._rail_factory or EvmRail)(chain=params.chain, signer=signer)
        try:
            tx = rail.build_erc20_transfer(token=token, to=to, amount_raw=amount_raw)
        except Exception as exc:
            return self._ar(error=f"could not build the transaction: {exc}")

        authorize = self._guard_fn or tx_guard.authorize

        # reserve() spans authorize -> broadcast -> record so two concurrent
        # transfers cannot both clear a nearly-exhausted cap.
        async with gate.reserve():
            # Turn-origin detection lives in this tier; core cannot import it
            # (layering ratchet), and tx_guard fails closed without it.
            from tools.controller.action_registration import _is_forged_or_autonomous_turn

            decision = authorize(intent, tx, holder=signer.address, gate=gate,
                                 execution_context=execution_context, tool_self=self,
                                 price_fn=self._price,
                                 forged_fn=_is_forged_or_autonomous_turn)

            header = (f"transfer {params.amount} {ident.symbol or token} -> {to}\n"
                      f"  simulated value: "
                      f"{'unknown' if decision.amount_usd is None else f'${decision.amount_usd:.4f}'}\n"
                      f"  guard: {decision.reason}\n"
                      f"  lane:  {decision.lane}\n")

            if not decision.allowed:
                return self._ar(content=header + "  RESULT: NOT SENT — nothing was broadcast.")

            # Same gas sizing as _run_guarded (§3a) — a transfer fits the
            # default, but the sized limit is uniformly more honest.
            if decision.sim_gas_used:
                try:
                    tx = rail.size_gas(tx, decision.sim_gas_used)
                except Exception as exc:
                    return self._ar(error=(
                        f"refused at gas sizing: {exc} — nothing was broadcast"))
                header += (f"  gas:   limit {tx.get('gas')} "
                           f"(simulation used {decision.sim_gas_used})\n")

            if params.dry_run:
                return self._ar(content=header + (
                    "  RESULT: DRY RUN — the guard would allow this, but nothing was "
                    "broadcast. Re-run with dry_run=false to send."))

            try:
                tx_hash = rail.sign_and_send(tx)
            except Exception as exc:
                return self._ar(error=f"broadcast failed: {exc} — funds were NOT sent")

            receipt = rail.await_receipt(tx_hash)
            gate.record(venue="defi", action="transfer",
                        amount_usd=decision.amount_usd or 0.0,
                        counterparty=to, idempotency_key=idem, result_ref=tx_hash)

        if receipt.succeeded:
            return self._ar(content=header + (
                f"  RESULT: SENT AND CONFIRMED\n"
                f"  tx: {tx_hash}\n  block: {receipt.block_number}"))
        if receipt.status == "pending":
            return self._ar(content=header + (
                f"  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. The "
                f"transaction may still land — do NOT retry blindly.\n  tx: {tx_hash}"))
        return self._ar(content=header + (
            f"  RESULT: REVERTED ON-CHAIN — the transfer did NOT happen, but gas "
            f"was spent.\n  tx: {tx_hash}"))

    # -- T4: allowance hygiene -------------------------------------------

    def _run_guarded(self, *, intent, tx, rail, gate, signer, execution_context,
                     header: str, dry_run: bool, venue_action: str, idem: str,
                     counterparty: str):
        """authorize -> broadcast -> confirm -> record, under one reservation.

        Extracted so approve/revoke/swap share EXACTLY the transfer path's
        guarantees instead of each re-deriving them. `reserve()` spans the whole
        span so two concurrent money verbs cannot both clear a nearly-exhausted
        cap.
        """
        from core.wallet import tx_guard
        authorize = self._guard_fn or tx_guard.authorize
        from tools.controller.action_registration import (
            _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)

        decision = authorize(intent, tx, holder=signer.address, gate=gate,
                             execution_context=execution_context, tool_self=self,
                             price_fn=self._price,
                             fallback_price_fn=self._fallback_price,
                             forged_fn=_is_forged_or_autonomous_turn,
                             autonomous_ok_fn=_is_autonomous_goal_turn)
        header += (f"  simulated value: "
                   f"{'unknown' if decision.amount_usd is None else f'${decision.amount_usd:.4f}'}\n"
                   f"  guard: {decision.reason}\n  lane:  {decision.lane}\n")
        if not decision.allowed:
            return self._ar(content=header + "  RESULT: NOT SENT — nothing was broadcast.")
        # Size the gas limit from the simulation's gasUsed (§3a): the fixed
        # default out-of-gas-reverts a swap on-chain and burns the fee. Done
        # before the dry-run return so a sizing refusal shows up in a dry run.
        if decision.sim_gas_used:
            try:
                tx = rail.size_gas(tx, decision.sim_gas_used)
            except Exception as exc:
                return self._ar(error=(
                    f"refused at gas sizing: {exc} — nothing was broadcast"))
            header += (f"  gas:   limit {tx.get('gas')} "
                       f"(simulation used {decision.sim_gas_used})\n")
        if dry_run:
            return self._ar(content=header + (
                "  RESULT: DRY RUN — the guard would allow this, but nothing was "
                "broadcast. Re-run with dry_run=false to send."))
        try:
            tx_hash = rail.sign_and_send(tx)
        except Exception as exc:
            return self._ar(error=f"broadcast failed: {exc} — nothing was sent")
        receipt = rail.await_receipt(tx_hash)
        # 2026-08-26 exit untying: an approve/revoke is a PRECONDITION, not a
        # spend — value leaves on the swap, which records the real number. The
        # old accounting charged one ticket to the daily cap twice (approve +
        # swap), and at the cap edge the approve landed and the swap then
        # refused ("a confirmed approve counts against the trailing-24h cap").
        # The grant is still bounded BEFORE it lands: tx_guard runs gate.check
        # against the grant's value, so an over-headroom approve never confirms.
        recorded_usd = (0.0 if venue_action in ("approve", "revoke")
                        else (decision.amount_usd or 0.0))
        gate.record(venue="defi", action=venue_action,
                    amount_usd=recorded_usd,
                    counterparty=counterparty, idempotency_key=idem,
                    result_ref=tx_hash)
        if receipt.succeeded:
            return self._ar(content=header + (
                f"  RESULT: CONFIRMED\n  tx: {tx_hash}\n  block: {receipt.block_number}"))
        if receipt.status == "pending":
            return self._ar(content=header + (
                f"  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. It may "
                f"still land — do NOT retry blindly.\n  tx: {tx_hash}"))
        return self._ar(content=header + (
            f"  RESULT: REVERTED ON-CHAIN — it did NOT happen, but gas was "
            f"spent.\n  tx: {tx_hash}"))

    @BaseTool.action(
        "Approve an EXACT token amount for a spender (e.g. a DEX router) before "
        "a swap. Unlimited approvals are refused. Revoke with revoke_approval "
        "when the trade is done. dry_run defaults to TRUE.",
        param_model=ApproveParams)
    async def approve_token(self, params: ApproveParams, execution_context=None):
        from core.wallet import tx_guard
        from core.wallet.broadcast.evm import EvmRail
        from core.wallet.tokens import get_token_identity, normalize_address
        from tools.defi.providers import univ3

        chain_err = _unsupported_chain(params.chain)
        if chain_err:
            return self._ar(error=chain_err)
        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
        try:
            token = normalize_address(params.token)
            spender = normalize_address(params.spender)
        except ValueError as exc:
            return self._ar(error=str(exc))

        ident = get_token_identity(params.chain, token)
        if ident.decimals is None:
            return self._ar(error=(
                f"{token} does not report decimals — refusing to compute an "
                f"allowance for a token whose denomination is unknown"))
        amount_raw = int(round(params.amount * (10 ** ident.decimals)))
        if amount_raw >= _UNLIMITED_APPROVAL_FLOOR:
            return self._ar(error=(
                "refused: that is an effectively UNLIMITED approval. An unlimited "
                "grant outlives the trade and remains a standing claim on the "
                "wallet. Approve the exact amount this trade needs."))

        # The infinite marker alone is not enough: 1e30 USDC is absurd yet sits
        # far below 2**128. Bound the approval by the USD the caller declared —
        # an allowance is a claim on funds, so it belongs under the same ceiling
        # as a spend.
        unit_price = self._price(params.chain, token)
        if unit_price:
            # Cents, not sub-cent noise (2026-08-26): $1.9906 at-risk against a
            # declared $1.99 was a live refuse/resize/retry dance at every cap
            # edge. Sub-cent drift is quote rounding, not risk.
            approval_usd = round(params.amount * unit_price, 2)
            if approval_usd > params.max_spend_usd:
                return self._ar(error=(
                    f"refused: this approval puts ${approval_usd:,.2f} at risk but "
                    f"you declared max_spend_usd=${params.max_spend_usd:,.2f}. "
                    f"Approve only what the trade needs."))

        signer = wallet.operational_signer()
        gate = wallet.policy
        idem = f"defi_approve:{params.chain}:{token}:{spender}:{amount_raw}:{uuid.uuid4().hex[:8]}"
        data = univ3.build_approve_data(spender=spender, amount_raw=amount_raw)
        rail = (self._rail_factory or EvmRail)(chain=params.chain, signer=signer)
        try:
            tx = rail.build_call(to=token, data=data, value=0)
        except Exception as exc:
            return self._ar(error=f"could not build the transaction: {exc}")

        # 028 (2026-08-22): the wallet's OWN held balance of `token`, so the
        # guard can waive the high-confidence price bar for an exit that
        # cannot exceed what is already owned. A read failure is None — no
        # exemption, same refusal as before 028 (fail closed, not open).
        try:
            held_balance_raw = self._held_balance_raw(params.chain, signer.address, token)
        except Exception:
            held_balance_raw = None

        # DECLARING the grant is what lets the guard verify it: an allowance the
        # simulation reveals but the intent did not declare is refused.
        intent = tx_guard.TxIntent(
            chain=params.chain, token=token, to=spender, amount_raw=0,
            max_spend_usd=params.max_spend_usd,
            expected_allowance_grants=((token, spender, amount_raw),),
            is_allowance_op=True, idempotency_key=idem,
            held_balance_raw=held_balance_raw)
        header = (f"approve {params.amount} {ident.symbol or token} for {spender}\n")
        async with gate.reserve():
            return self._run_guarded(
                intent=intent, tx=tx, rail=rail, gate=gate, signer=signer,
                execution_context=execution_context, header=header,
                dry_run=params.dry_run, venue_action="approve", idem=idem,
                counterparty=spender)

    @BaseTool.action(
        "Set a spender's allowance to ZERO. Use after a swap so no standing "
        "claim on the wallet survives the trade. dry_run defaults to TRUE.",
        param_model=RevokeParams)
    async def revoke_approval(self, params: RevokeParams, execution_context=None):
        from core.wallet import tx_guard
        from core.wallet.broadcast.evm import EvmRail
        from core.wallet.tokens import get_token_identity, normalize_address
        from tools.defi.providers import univ3

        chain_err = _unsupported_chain(params.chain)
        if chain_err:
            return self._ar(error=chain_err)
        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
        try:
            token = normalize_address(params.token)
            spender = normalize_address(params.spender)
        except ValueError as exc:
            return self._ar(error=str(exc))

        ident = get_token_identity(params.chain, token)
        signer = wallet.operational_signer()
        gate = wallet.policy
        idem = f"defi_revoke:{params.chain}:{token}:{spender}:{uuid.uuid4().hex[:8]}"
        data = univ3.build_approve_data(spender=spender, amount_raw=0)
        rail = (self._rail_factory or EvmRail)(chain=params.chain, signer=signer)
        try:
            tx = rail.build_call(to=token, data=data, value=0)
        except Exception as exc:
            return self._ar(error=f"could not build the transaction: {exc}")

        # A revoke grants nothing, so it declares no allowance and needs no USD
        # headroom; the guard still simulates it and refuses any hidden grant.
        intent = tx_guard.TxIntent(
            chain=params.chain, token=token, to=spender, amount_raw=0,
            max_spend_usd=0.01, expected_allowance_grants=(),
            is_allowance_op=True, idempotency_key=idem)
        header = f"revoke {ident.symbol or token} allowance for {spender}\n"
        async with gate.reserve():
            return self._run_guarded(
                intent=intent, tx=tx, rail=rail, gate=gate, signer=signer,
                execution_context=execution_context, header=header,
                dry_run=params.dry_run, venue_action="revoke", idem=idem,
                counterparty=spender)

    @BaseTool.action(
        "Swap one token for another. Finds a route (Uniswap V3 first, then a "
        "DEX aggregator if the operator enabled one — which reaches Aerodrome "
        "and V2-fork pools that V3 cannot), bounds slippage, cross-checks the "
        "route against an independent price, and simulates before anything is "
        "broadcast. Requires an allowance — call approve_token first with the "
        "spender the refusal names, and revoke_approval after. "
        "dry_run defaults to TRUE.",
        param_model=SwapParams)
    async def swap(self, params: SwapParams, execution_context=None):
        from core.wallet import tx_guard
        from core.wallet.broadcast.evm import EvmRail
        from core.wallet.tokens import get_token_identity, normalize_address

        # The chain refusal comes FIRST: "solana is read-only here" is true
        # whatever the wallet's state, while "agent wallet not enabled" implies
        # enabling it would help, which on a read-only chain it would not.
        chain_err = _unsupported_chain(params.chain) or _no_swap_route(params.chain)
        if chain_err:
            return self._ar(error=chain_err)
        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
        try:
            token_in = normalize_address(params.token_in)
            token_out = normalize_address(params.token_out)
        except ValueError as exc:
            return self._ar(error=str(exc))
        if token_in == token_out:
            return self._ar(error="token_in and token_out are the same token")

        id_in = get_token_identity(params.chain, token_in)
        id_out = get_token_identity(params.chain, token_out)
        if id_in.decimals is None or id_out.decimals is None:
            return self._ar(error=(
                "one side does not report decimals — refusing to size a swap "
                "against a token whose denomination is unknown"))
        amount_in_raw = int(round(params.amount_in * (10 ** id_in.decimals)))

        signer = wallet.operational_signer()
        gate = wallet.policy
        slippage = params.slippage_bps or _max_slippage_bps()

        # 028 (2026-08-22): the wallet's OWN held balance of token_in, so the
        # guard can waive the high-confidence price bar for a sell that
        # cannot exceed what is already owned. A read failure is None — no
        # exemption, same refusal as before 028 (fail closed, not open).
        # Read BEFORE routing (2026-08-26) so a full exit can clamp below.
        try:
            held_balance_raw = self._held_balance_raw(params.chain, signer.address, token_in)
        except Exception:
            held_balance_raw = None

        # Full-exit clamp (2026-08-26): a declared amount within 1% ABOVE the
        # held balance is a rounding overshoot, not a wrong size — the live
        # case was selling 28.925134 against a 28.92513399… balance, refused
        # by the token contract (STF) and retried by hand at lower precision.
        # A real overshoot (>1%) keeps the declaration and fails honestly.
        if (held_balance_raw is not None and held_balance_raw > 0
                and held_balance_raw < amount_in_raw <= int(held_balance_raw * 1.01)):
            amount_in_raw = held_balance_raw

        # The recipient is baked into the calldata, so the route must be built
        # for THIS signer — a route quoted for another address would send the
        # output somewhere else.
        route, route_why = self._route(params.chain, token_in, token_out,
                                       amount_in_raw, holder=signer.address,
                                       slippage_bps=slippage)
        if route is None:
            return self._ar(error=(
                f"cannot route {id_in.symbol or token_in} -> "
                f"{id_out.symbol or token_out}: {route_why}"))

        # Freshness window (§1.3): quoted_at is enforced, not decorative. An
        # aggregator quote is MORE perishable than a pool read, not less.
        age = time.time() - route.quoted_at
        if age > _QUOTE_MAX_AGE_SEC:
            return self._ar(error=(
                f"refused: the quote is stale ({age:.0f}s old, max "
                f"{_QUOTE_MAX_AGE_SEC:.0f}s) — prices move; re-quote and execute "
                f"promptly. Nothing was broadcast."))

        amount_out_min = route.amount_out_min_raw
        if not amount_out_min or amount_out_min <= 0:
            return self._ar(error="the route carries no minimum output — refused")

        # `spender` holds the allowance; `to` is the call target. They are the
        # same contract on both providers today, but they are separate fields on
        # purpose — conflating them is exactly the class of mistake the chain
        # registry exists to prevent, so each is used for its own job.
        spender = route.spender

        # An allowance is required before the spender can pull token_in. Refuse
        # with the exact remedy rather than broadcasting a call that reverts and
        # burns gas.
        from tools.defi.providers import univ3
        allowance = univ3.read_allowance(params.chain, token_in, signer.address,
                                         spender)
        if allowance is not None and allowance < amount_in_raw:
            return self._ar(error=(
                f"insufficient allowance: the spender may pull {allowance} but this "
                f"swap needs {amount_in_raw}. Call approve_token(token="
                f"{token_in}, spender={spender}, amount={params.amount_in}) "
                f"first, then swap, then revoke_approval."))

        idem = (f"defi_swap:{params.chain}:{token_in}:{token_out}:"
                f"{amount_in_raw}:{uuid.uuid4().hex[:8]}")
        rail = (self._rail_factory or EvmRail)(chain=params.chain, signer=signer)
        try:
            tx = rail.build_call(to=route.to, data=route.calldata,
                                 value=route.value_raw)
        except Exception as exc:
            return self._ar(error=f"could not build the transaction: {exc}")

        # The swap spends token_in and grants nothing; any allowance the
        # simulation reveals is undeclared and the guard refuses it. The
        # spender pair is MEASURED (watch_spenders) so its read delta — a
        # decrease when the spender pulls token_in — is judged as a decrease,
        # not mistaken for a grant by the event-log cross-check. Declaring
        # `inflow_token` (2026-08-26) lets the guard value an unpriceable exit
        # at the MEASURED receipt and recognize exit-shaped intents for the
        # DEFI_MONITOR_EXITS lane.
        intent = tx_guard.TxIntent(
            chain=params.chain, token=token_in, to=spender,
            amount_raw=amount_in_raw, max_spend_usd=params.max_spend_usd,
            expected_allowance_grants=(), watch_spenders=(spender,),
            idempotency_key=idem, held_balance_raw=held_balance_raw,
            inflow_token=token_out)

        out_human = route.amount_out_raw / (10 ** id_out.decimals)
        min_human = amount_out_min / (10 ** id_out.decimals)
        sanity_verdict, sanity_note = self._route_sanity(params.chain, route,
                                                         id_in, id_out)
        if sanity_verdict == "DISAGREES":
            # §1.2: a disagreeing route BLOCKS — it used to only narrate. A pool
            # price is a number anyone with capital can seed; when it disagrees
            # with an independent source, executing anyway is how a thin or
            # manipulated pool extracts value bounded only by amount_in.
            return self._ar(error=(
                f"refused: route check DISAGREES — {sanity_note}. The pool is "
                f"quoting a price the independent source does not support "
                f"(drift above {_route_drift_max_pct():.0f}%); a thin or "
                f"manipulated pool extracts value this way. Nothing was "
                f"broadcast."))
        header = (
            f"swap {params.amount_in} {id_in.symbol or token_in} -> "
            f"{id_out.symbol or token_out}\n"
            f"  route: {route.venue}\n"
            f"  quoted out: {out_human:.8f}  (min after {slippage}bps slippage: "
            f"{min_human:.8f})\n"
            f"  {sanity_note}\n")

        async with gate.reserve():
            return self._run_guarded(
                intent=intent, tx=tx, rail=rail, gate=gate, signer=signer,
                execution_context=execution_context, header=header,
                dry_run=params.dry_run, venue_action="swap", idem=idem,
                counterparty=spender)

    @BaseTool.action(
        "Swap one SPL token for another on SOLANA via the Jupiter aggregator. "
        "Simulated and asserted before anything is broadcast. There is NO "
        "allowance to grant or revoke on Solana — Jupiter needs none — so this "
        "is a single verb, not the EVM approve/swap/revoke cycle. dry_run "
        "defaults to TRUE.",
        param_model=SolanaSwapParams)
    async def solana_swap(self, params: SolanaSwapParams, execution_context=None):
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
        from core.wallet.addresses import normalize_for_chain
        from core.wallet.solana_simulation import is_plausible_rent

        if not _solana_trade_enabled():
            return self._ar(error=(
                "solana trading is off. Set SOLANA_TRADE_ENABLED=true to arm "
                "it. Read-only Solana verbs (token_info, price, new_pools, "
                "trending, portfolio) work regardless."))
        try:
            token_in = normalize_for_chain("solana", params.token_in)
            token_out = normalize_for_chain("solana", params.token_out)
        except ValueError as exc:
            return self._ar(error=str(exc))
        if token_in == token_out:
            return self._ar(error="token_in and token_out are the same token")

        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
        try:
            signer = wallet.solana_signer()
        except Exception as exc:
            return self._ar(error=f"no Solana signer: {exc}")
        gate = getattr(wallet, "policy", None)
        if gate is None:
            return self._ar(error=(
                "refused: the wallet exposes no PolicyGate — a money verb may "
                "not run ungoverned"))

        slippage = params.slippage_bps or _max_slippage_bps()
        # Jupiter quotes in RAW units; the caller gives a human amount, and we
        # have no on-chain decimals reader for SPL yet, so the caller's amount
        # is treated as already-scaled by the mint's decimals via token_info.
        ident = self._identity_solana(token_in)
        if ident is None:
            return self._ar(error=(
                f"cannot read decimals for {token_in} — refusing to size a swap "
                f"against a token whose denomination is unknown. A guessed "
                f"denomination misprices a trade by orders of magnitude."))
        amount_in_raw = int(round(params.amount_in * (10 ** ident)))

        # Held balance of the outflow token, read LAZILY (only the exit lanes
        # and the unpriceable-valuation ladder need it) and memoized.
        _held_cache: dict = {}

        def _held_raw():
            if "v" not in _held_cache:
                _held_cache["v"] = self._solana_held_raw(signer.address, token_in)
            return _held_cache["v"]

        usdc_mint = self._solana_usdc_mint()

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
        refusal, autonomous_origin, monitor_exit = self._solana_turn_gate(
            execution_context, exit_shaped_fn=_exit_shaped)
        if refusal:
            return self._ar(error=refusal)

        quote = self._solana_quote(token_in, token_out, amount_in_raw, slippage)
        if quote is None:
            return self._ar(error=(
                f"no route for {token_in} -> {token_out} on solana, or the "
                f"lookup failed. Either way this is UNKNOWN, not a zero-value "
                f"trade — retry before concluding the token is unreachable."))

        from tools.defi.providers import jupiter
        floor = jupiter.verified_floor(quote.raw, slippage_bps=slippage)
        if floor is None:
            return self._ar(error=(
                f"refused: Jupiter's own minimum output does not clear your "
                f"{slippage}bps slippage bound. Jupiter bakes its minimum into "
                f"the transaction it builds, so a looser one cannot be "
                f"rewritten — only refused."))

        raw_tx = self._solana_build(quote, signer.address)
        if raw_tx is None:
            return self._ar(error=(
                "Jupiter could not build a transaction for this route (often an "
                "unfunded or unexpected token-account state). Nothing was sent."))

        deltas = self._solana_simulate(raw_tx=raw_tx, owner=signer.address,
                                      mints=(token_in, token_out))
        if deltas is None or not deltas.ok:
            reason = getattr(deltas, "reason", "no result") if deltas else "no result"
            return self._ar(error=(
                f"refused: the simulation did not pass ({reason}). A simulation "
                f"that did not run is not a simulation that passed."))

        # The Solana replacement for the undeclared-Approval refusal. A swap
        # grants nothing: any delegate, close authority, ownership change or
        # freeze the simulation reveals is undeclared by definition.
        if deltas.grants_authority():
            return self._ar(error=(
                f"refused: this transaction changes AUTHORITY over your "
                f"accounts — {list(deltas.authority_grants)}. A swap grants "
                f"nothing; an authority change is a future drain the swap "
                f"itself does not perform. Nothing was broadcast."))

        # A swap that moves NO tokens is not a swap. Passing here would be
        # passing because we observed nothing, which is indistinguishable from
        # passing because nothing moved — and the first is a broken simulation.
        if not deltas.token_deltas:
            return self._ar(error=(
                "refused: the simulation observed NO token movement for this "
                "swap. A swap that moves nothing is not a swap, and an empty "
                "delta set cannot be told apart from an unobserved one. "
                "Nothing was broadcast."))

        # Rent is classified, not licensed (Solana research mismatch #5).
        if not is_plausible_rent(deltas.native_delta):
            return self._ar(error=(
                f"refused: the transaction moves {deltas.native_delta} lamports "
                f"of SOL, far more than account rent and fees explain. Nothing "
                f"was broadcast."))

        # An UNOBSERVED declared mint fails CLOSED. `token_deltas` being
        # non-empty only says SOMETHING moved; if the declared outflow mint is
        # not in it, the outflow assertion below simply does not run, and a
        # check that did not run must never read as a check that passed. That
        # was live: a Token-2022 mint's associated account was derived under
        # the classic SPL Token program, so the simulation observed no state
        # for it and `in_delta` came back None — silently skipping the one
        # assertion that bounds how much may leave.
        if token_in not in deltas.token_deltas:
            return self._ar(error=(
                f"refused: the simulation could not observe token {token_in} — "
                f"it moved {list(deltas.token_deltas)} but says nothing about "
                f"the token you declared as leaving. The outflow assertion "
                f"cannot run against an unobserved account, and a check that "
                f"did not run is not a check that passed. Nothing was "
                f"broadcast."))

        # Delta assertion, sell side (tx_guard step 6 mirror): the measured
        # outflow of token_in may not exceed the declared amount (0.1% + 1 raw
        # tolerance for routing dust).
        in_delta = deltas.token_deltas.get(token_in)
        if in_delta is not None and in_delta < 0:
            outflow_raw = -in_delta
            if outflow_raw > amount_in_raw + max(1, amount_in_raw // 1000):
                return self._ar(error=(
                    f"refused: the simulation shows {outflow_raw} raw of "
                    f"{token_in} leaving, but only {amount_in_raw} was declared "
                    f"— the transaction does more than the declared swap. "
                    f"Nothing was broadcast."))

        out_delta_raw = deltas.token_deltas.get(token_out)
        if monitor_exit and not (out_delta_raw and out_delta_raw > 0):
            return self._ar(error=(
                "refused: the monitor-exit lane requires a measured inflow of "
                "the declared receive token — the simulation shows none, so "
                "this is not an exit. Nothing was broadcast."))

        # Valuation (tx_guard step 7 mirror): price the outflow, or — for an
        # exit within the held balance that sells into the chain's USDC — value
        # it at the simulation's measured receipt. Unpriceable refuses; no cap
        # can bound a number you do not have. Caps operate in cents.
        amount_usd = self._solana_value_usd(
            token_in=token_in, amount_in=params.amount_in, token_out=token_out,
            out_delta_raw=out_delta_raw, usdc_mint=usdc_mint,
            exit_bounded_fn=lambda: (_held_raw() is not None
                                     and amount_in_raw <= _held_raw()))
        if amount_usd is None:
            return self._ar(error=(
                "refused: the outflow could not be valued in USD by any "
                "source, so no cap can bound it. A sell of a held token into "
                "the chain's USDC is valued at the simulation's measured "
                "receipt; anything else refuses. Nothing was broadcast."))
        declared = round(params.max_spend_usd, 2)
        if amount_usd > declared:
            return self._ar(error=(
                f"refused: ${amount_usd:.2f} exceeds the declared "
                f"max_spend_usd ${declared:.2f}. Nothing was broadcast."))

        header = (f"solana swap {params.amount_in} {token_in} -> {token_out}\n"
                  f"  route: {quote.venue}\n"
                  f"  quoted out: {quote.amount_out_raw}  (min {floor})\n"
                  f"  simulated: token deltas {deltas.token_deltas}, "
                  f"native {deltas.native_delta} lamports\n"
                  f"  valued: ${amount_usd:.2f} (declared max ${declared:.2f})\n")

        from core.wallet import tx_guard
        idem = (f"defi_solana_swap:{token_in}:{token_out}:{amount_in_raw}:"
                f"{uuid.uuid4().hex[:8]}")
        # reserve() spans check -> broadcast -> record so two concurrent money
        # verbs cannot both clear a nearly-exhausted cap (EVM parity).
        async with gate.reserve():
            # PolicyGate: kill-switch (fail-closed inside check), per-tx
            # ceiling, rolling daily + venue caps, replay guard.
            verdict = gate.check(venue="defi", amount_usd=amount_usd,
                                 idempotency_key=idem)
            if not verdict.allowed:
                return self._ar(content=header + (
                    f"  guard: refused by PolicyGate: {verdict.reason}\n"
                    f"  RESULT: NOT SENT — nothing was broadcast."))
            # tx_guard step 9 mirror: the autonomous ceiling and the
            # daily-cap-required bar for unattended origins.
            if amount_usd > tx_guard.autonomous_max_usd():
                return self._ar(content=header + (
                    f"  guard: owner approval required: ${amount_usd:.2f} is "
                    f"above the autonomous ceiling "
                    f"${tx_guard.autonomous_max_usd():.2f}\n"
                    f"  lane:  owner_queue\n"
                    f"  RESULT: NOT SENT — nothing was broadcast."))
            if autonomous_origin and not getattr(gate, "has_daily_cap", False):
                return self._ar(content=header + (
                    "  guard: refused — unattended trading needs an aggregate "
                    "damage bound; set WALLET_DAILY_CAP_USD\n"
                    "  lane:  owner_queue\n"
                    "  RESULT: NOT SENT — nothing was broadcast."))
            header += "  lane:  autonomous\n"
            if monitor_exit:
                logger.info(
                    "defi.solana_swap monitor_exit token_in=%s token_out=%s "
                    "amount_usd=%.2f — forged turn allowed for an EXIT-shaped "
                    "swap (DEFI_MONITOR_EXITS)", token_in, token_out, amount_usd)
            if params.dry_run:
                return self._ar(content=header + "\n[DRY RUN] nothing was broadcast.")

            # RPC trust (tx_guard step 4 mirror): the simulation, the deltas
            # and the caps all read from the RPC, so the shared public endpoint
            # cannot be the trust anchor for a broadcast. Dry runs above are a
            # $0 read and stay available unpinned.
            import os as _os
            if not _os.getenv("DEFI_SOLANA_RPC", "").strip():
                return self._ar(error=(
                    "refused: no pinned RPC for solana. The simulation, the "
                    "deltas and the caps all read from it, so the shared "
                    "public endpoint cannot be the trust anchor for moving "
                    "funds — set DEFI_SOLANA_RPC. Dry runs are unaffected."))

            try:
                signature = self._solana_send(raw_tx, signer)
            except Exception as exc:
                return self._ar(error=f"broadcast failed: {exc}")
            gate.record(venue="defi", action="solana_swap",
                        amount_usd=amount_usd, counterparty=token_out,
                        idempotency_key=idem, result_ref=signature)
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
                self._solana_confirm, signature)
        except Exception as exc:
            confirmed, detail = False, f"status read failed: {exc}"
        outcome = confirmation_outcome(confirmed, detail)
        if outcome == "confirmed":
            return self._ar(content=header + (
                f"\n  RESULT: CONFIRMED ({detail})\n  sig: {signature}"))
        if outcome == "reverted":
            return self._ar(content=header + (
                f"\n  RESULT: REVERTED ON-CHAIN — it did NOT happen, but the "
                f"fee was spent.\n  sig: {signature}\n  detail: {detail}"))
        return self._ar(content=header + (
            f"\n  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. It "
            f"may still land, or the blockhash may have expired — do NOT retry "
            f"blindly; check the signature first.\n  sig: {signature}\n"
            f"  detail: {detail}"))

    def _solana_turn_gate(self, execution_context, *, exit_shaped_fn):
        """tx_guard steps 1-2, mirrored for the SVM path (which has no EVM
        transaction to route through ``tx_guard.authorize``).

        Returns ``(refusal | None, autonomous_origin, monitor_exit)``. Fails
        CLOSED on every probe error, exactly like the EVM guard.
        """
        from core.wallet import tx_guard
        try:
            if tx_guard._halted():
                return ("refused: autonomy is HALTED (owner kill-switch) — "
                        "nothing was broadcast", False, False)
        except Exception as exc:
            return (f"refused: kill-switch probe failed ({exc}); failing "
                    f"closed", False, False)
        # tx_guard step 1b mirror: owner entry-pause. Exit-shaped intents
        # still pass — the pause is about not adding NEW risk.
        try:
            if tx_guard._entry_paused():
                try:
                    exit_shaped = bool(exit_shaped_fn())
                except Exception:
                    exit_shaped = False
                if not exit_shaped:
                    return ("refused: new treasury entries are PAUSED (owner "
                            "entry-pause) — exits still run; clear with "
                            "`polyrob owner resume-entries`", False, False)
        except Exception as exc:
            return (f"refused: entry-pause probe failed ({exc}); failing "
                    f"closed", False, False)
        if execution_context is None:
            # Owner-direct / CLI / programmatic call — parity with the EVM
            # verbs and crypto_trade_gate (flag + cap gates still apply).
            return (None, False, False)
        try:
            from tools.controller.action_registration import (
                _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)
            forged = _is_forged_or_autonomous_turn(execution_context, self)
        except Exception as exc:
            return (f"refused: could not prove the turn is genuine ({exc})",
                    False, False)
        if not forged:
            return (None, False, False)
        if tx_guard._autonomous_turn_allowed(execution_context, self,
                                             _is_autonomous_goal_turn):
            return (None, True, False)
        try:
            exit_shaped = bool(exit_shaped_fn())
        except Exception:
            exit_shaped = False
        if (tx_guard.monitor_exits_enabled()
                and not getattr(execution_context, "is_sub_agent", False)
                and getattr(execution_context, "role", "leaf") == "orchestrator"
                and exit_shaped):
            # The exit-only carve-out (DEFI_MONITOR_EXITS): a forged MAIN-agent
            # turn may CLOSE a position into the quote asset. It rides the
            # autonomous origin so the daily-cap-required bar still applies,
            # and the measured-inflow assertion runs after simulation.
            return (None, True, True)
        return (("refused: a forged/autonomous turn (self-wake, "
                 "delegation-result, leaf, or autonomous run) cannot move "
                 "funds"), False, False)

    def _solana_usdc_mint(self):
        from core.wallet import chains
        row = chains.get("solana")
        return row.usdc if row else None

    def _solana_held_raw(self, owner: str, mint: str):
        """Held balance of *mint* in raw units. None = UNKNOWN (never zero).

        wSOL folds in the native SOL balance — Jupiter wraps through it, so the
        balance behind a SOL sell is native SOL plus any wrapped account.
        """
        if self._solana_held_fn:
            try:
                return self._solana_held_fn(owner, mint)
            except Exception:
                return None
        from core.wallet import solana_onchain
        try:
            balances = solana_onchain.token_balances(owner)
        except Exception:
            balances = None
        if mint == _WSOL_MINT:
            try:
                sol = solana_onchain.native_balance(owner)
            except Exception:
                sol = None
            if sol is None:
                return None
            return int(sol * 1_000_000_000) + int((balances or {}).get(mint, 0))
        if balances is None:
            return None
        return int(balances.get(mint, 0))

    def _solana_value_usd(self, *, token_in, amount_in, token_out,
                          out_delta_raw, usdc_mint, exit_bounded_fn):
        """USD value of the outflow, EVM-parity ladder (tx_guard step 7):
        pinned USDC = $1.00 by definition → high-confidence price →
        (exit-bounded) fallback price → (exit-bounded sell-to-USDC) the
        simulation's measured quote inflow. None = unpriceable → refuse.
        """
        if usdc_mint and token_in == usdc_mint:
            return round(float(amount_in), 2)
        try:
            px = self._price("solana", token_in)
        except Exception:
            px = None
        exit_bounded = False
        if px is None:
            try:
                exit_bounded = bool(exit_bounded_fn())
            except Exception:
                exit_bounded = False
            if exit_bounded:
                try:
                    px = self._fallback_price("solana", token_in)
                except Exception:
                    px = None
        if px is not None:
            return round(float(amount_in) * float(px), 2)
        if (exit_bounded and usdc_mint and token_out == usdc_mint
                and out_delta_raw and out_delta_raw > 0):
            out_dec = self._identity_solana(token_out)
            if out_dec is not None:
                value = round(out_delta_raw / (10 ** out_dec), 2)
                logger.info(
                    "defi.solana_swap exit_inflow_valuation token_in=%s "
                    "amount_usd=%.2f — outflow unpriceable by any source; caps "
                    "run against the measured USDC receipt", token_in, value)
                return value
        return None

    def _identity_solana(self, mint: str) -> Optional[int]:
        """Decimals for an SPL mint, READ from the chain, or None to refuse.

        This used to default to 6, which was a guess on a SIZING path and wrong
        for a large share of mints — wSOL is 9, so a 0.003 SOL swap sized at 6
        decimals becomes 0.000003 SOL, a 1000x error. The EVM path refuses in
        exactly this situation ("refusing to size a swap against a token whose
        denomination is unknown") and this was the one place the Solana path
        guessed instead. `getTokenSupply` returns the mint's decimals directly,
        so there was never a reason to.
        """
        if self._solana_decimals_fn:
            return self._solana_decimals_fn(mint)
        try:
            from core.wallet import solana_onchain
            res = solana_onchain._rpc("getTokenSupply", [mint])
            decimals = ((res or {}).get("value") or {}).get("decimals")
            return int(decimals) if decimals is not None else None
        except Exception:
            return None

    def _solana_quote(self, token_in, token_out, amount_in_raw, slippage_bps):
        if self._solana_quote_fn:
            return self._solana_quote_fn(token_in, token_out, amount_in_raw,
                                         slippage_bps=slippage_bps)
        from tools.defi.providers import jupiter
        return jupiter.quote(token_in, token_out, amount_in_raw,
                             slippage_bps=slippage_bps)

    def _solana_build(self, quote, holder):
        if self._solana_build_fn:
            return self._solana_build_fn(quote, holder)
        from tools.defi.providers import jupiter
        return jupiter.build_swap(quote.raw, holder)

    def _solana_simulate(self, *, raw_tx, owner, mints=()):
        """Vet the bytes, then simulate them against everything we own.

        ⚠️ The addresses MUST be the token accounts, not the owner. SPL
        balances live in accounts the owner merely owns, so asking for the
        owner's system account returns no token balances and `parse_deltas`
        then sees an EMPTY delta set — which reads as "nothing moved" and
        passes. Observing nothing is not the same as verifying nothing moved,
        and that distinction is the whole point of simulating.

        The address set, the Token-2022-aware ATA derivation and the top-level
        program allowlist all live in `core.wallet.solana_tx_inspect` — the
        earlier inline version derived the ATA under the classic SPL Token
        program alone (so a Token-2022 mint was never observed) and named only
        the accounts the CALLER declared (so an instruction touching a
        different wallet-owned account was invisible).
        """
        if self._solana_simulate_fn:
            return self._solana_simulate_fn(raw_tx=raw_tx, owner=owner)
        from core.wallet import solana_tx_inspect
        from core.wallet.solana_rail import SolanaRail
        rail = SolanaRail(signer=None)
        return solana_tx_inspect.simulate(
            raw_tx, owner=owner, mints=mints, rpc=rail._rpc)

    def _solana_confirm(self, signature):
        """``(ok, detail)`` for a broadcast signature — the EVM `await_receipt`
        mirror. Bounded (the rail polls a fixed number of attempts) and never
        resends: an UNKNOWN status usually means an expired blockhash, and a
        blind retry is how a double-send happens.
        """
        if self._solana_confirm_fn:
            return self._solana_confirm_fn(signature)
        from core.wallet.solana_rail import SolanaRail
        return SolanaRail(signer=None).confirm(signature)

    def _solana_send(self, raw_tx, signer):
        if self._solana_send_fn:
            return self._solana_send_fn(raw_tx)
        from solders.transaction import VersionedTransaction
        from core.wallet.solana_rail import SolanaRail
        tx = VersionedTransaction.from_bytes(bytes(raw_tx))
        signed = signer.sign_transaction(tx)     # refuses a foreign fee payer
        return SolanaRail(signer=signer).send_raw(bytes(signed))

    def _route_sanity(self, chain, route, id_in, id_out) -> Tuple[str, str]:
        """(verdict, note): the route's implied price vs an INDEPENDENT source.

        Verdicts: ``AGREES`` (drift within ``_ROUTE_DRIFT_MAX_PCT``),
        ``DISAGREES`` (the caller REFUSES to execute — §1.2), ``UNAVAILABLE``
        (no independent price for one side; the caller proceeds, because the
        spend side is still priced and capped by the guard, and an unpriceable
        token_in refuses there — but the note must stay loud, never read as
        "route verified").

        A pool price is a number anyone with capital can seed, so agreement is
        not proof — but a wide disagreement is strong evidence the route is
        thin or manipulated, and that is now a refusal, not a narration.
        """
        try:
            price_in = self._price(chain, route.token_in)
            price_out = self._price(chain, route.token_out)
            if not price_in or not price_out:
                return ("UNAVAILABLE",
                        "route check: UNAVAILABLE — no independent price for one "
                        "side; the route is unverified")
            amount_in_human = route.amount_in_raw / (10 ** id_in.decimals)
            amount_out_human = route.amount_out_raw / (10 ** id_out.decimals)
            if amount_out_human <= 0:
                return ("UNAVAILABLE", "route check: UNAVAILABLE — zero output")
            # What this route actually charges per unit of token_out, in USD.
            route_price = (amount_in_human * price_in) / amount_out_human
            drift = abs(route_price - price_out) / price_out * 100.0
            verdict = "AGREES" if drift <= _route_drift_max_pct() else "DISAGREES"
            return (verdict,
                    f"route check: {verdict} — route implies "
                    f"${route_price:,.8f}/{id_out.symbol or 'token'} vs independent "
                    f"${price_out:,.8f} ({drift:.2f}% drift)")
        except Exception:
            return ("UNAVAILABLE",
                    "route check: UNAVAILABLE — the route is unverified")
