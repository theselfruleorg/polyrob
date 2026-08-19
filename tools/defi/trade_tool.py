"""defi_trade — the agent's on-chain money verbs (proposal 023 T3 + T4).

Verbs: `transfer` (T3), and `approve_token` / `revoke_approval` / `swap` (T4).
`transfer` came first because it is the simplest irreversible action and so the
right thing to prove the rail with; T5 (`contract_write`, arbitrary calls) is
still unbuilt.

Swaps route through Uniswap V3 (`providers/univ3.py`). Proposal 023 named 0x,
but 0x API v2 now requires a key we do not hold — reading the pools directly is
also strictly better here, because the quote comes from the same contracts that
execute it.

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
_ROUTE_DRIFT_MAX_PCT = 3.0

#: A quote older than this refuses to execute. swap() re-quotes inline today,
#: so this never trips — it exists so a future split of quote and execute
#: (e.g. an owner-approval lane) cannot silently execute a stale price.
_QUOTE_MAX_AGE_SEC = 30.0


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


class DefiTradeTool(BaseTool):
    def __init__(self, name: str = "defi_trade", config=None, container=None, *,
                 wallet=None, rail_factory=None, guard_fn=None, price_fn=None,
                 quote_fn=None):
        super().__init__(name=name, config=config if config is not None else _NULL_CONFIG,
                         container=container)
        self._wallet = wallet
        self._rail_factory = rail_factory
        self._guard_fn = guard_fn
        self._price_fn = price_fn
        self._quote_fn_override = quote_fn

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

    def _quote_fn(self, chain, token_in, token_out, amount_in_raw):
        if self._quote_fn_override:
            return self._quote_fn_override(chain, token_in, token_out, amount_in_raw)
        from tools.defi.providers import univ3
        return univ3.best_quote(chain, token_in, token_out, amount_in_raw)

    def _price(self, chain, addr):
        if self._price_fn:
            return self._price_fn(chain, addr)
        from tools.defi.providers import dexscreener
        info = dexscreener.token(chain, addr)
        # Only a trustworthy price may bound a cap — a thin, attacker-seedable
        # pool is not a price for this purpose.
        return info.price_usd if info.confidence == "high" else None

    @BaseTool.action(
        "Send tokens from the agent wallet to an address. Simulated and asserted "
        "against your declared max_spend_usd before anything is broadcast. "
        "dry_run defaults to TRUE — set it false to actually move funds.",
        param_model=TransferParams)
    async def transfer(self, params: TransferParams, execution_context=None):
        from core.wallet import tx_guard
        from core.wallet.broadcast.evm import EvmRail
        from core.wallet.tokens import get_token_identity, normalize_address

        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
        chain_err = _unsupported_chain(params.chain)
        if chain_err:
            return self._ar(error=chain_err)

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
        from tools.controller.action_registration import _is_forged_or_autonomous_turn

        decision = authorize(intent, tx, holder=signer.address, gate=gate,
                             execution_context=execution_context, tool_self=self,
                             price_fn=self._price,
                             forged_fn=_is_forged_or_autonomous_turn)
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
        gate.record(venue="defi", action=venue_action,
                    amount_usd=decision.amount_usd or 0.0,
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

        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
        chain_err = _unsupported_chain(params.chain)
        if chain_err:
            return self._ar(error=chain_err)
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
            approval_usd = params.amount * unit_price
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

        # DECLARING the grant is what lets the guard verify it: an allowance the
        # simulation reveals but the intent did not declare is refused.
        intent = tx_guard.TxIntent(
            chain=params.chain, token=token, to=spender, amount_raw=0,
            max_spend_usd=params.max_spend_usd,
            expected_allowance_grants=((token, spender, amount_raw),),
            is_allowance_op=True, idempotency_key=idem)
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

        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
        chain_err = _unsupported_chain(params.chain)
        if chain_err:
            return self._ar(error=chain_err)
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
        "Swap one token for another on Uniswap V3 (Base). Quotes the best fee "
        "tier, bounds slippage, cross-checks the route against an independent "
        "price, and simulates before anything is broadcast. Requires an "
        "allowance — call approve_token first, and revoke_approval after. "
        "dry_run defaults to TRUE.",
        param_model=SwapParams)
    async def swap(self, params: SwapParams, execution_context=None):
        from core.wallet import tx_guard
        from core.wallet.broadcast.evm import EvmRail
        from core.wallet.tokens import get_token_identity, normalize_address
        from tools.defi.providers import univ3

        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
        chain_err = _unsupported_chain(params.chain) or _no_swap_route(params.chain)
        if chain_err:
            return self._ar(error=chain_err)
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

        quote = self._quote_fn(params.chain, token_in, token_out, amount_in_raw)
        if quote is None:
            return self._ar(error=(
                f"no Uniswap V3 route for {id_in.symbol or token_in} -> "
                f"{id_out.symbol or token_out} at any fee tier. The pair may have "
                f"no pool on this chain."))

        # Freshness window (§1.3): quoted_at is enforced, not decorative.
        age = time.time() - quote.quoted_at
        if age > _QUOTE_MAX_AGE_SEC:
            return self._ar(error=(
                f"refused: the quote is stale ({age:.0f}s old, max "
                f"{_QUOTE_MAX_AGE_SEC:.0f}s) — prices move; re-quote and execute "
                f"promptly. Nothing was broadcast."))

        slippage = params.slippage_bps or _max_slippage_bps()
        amount_out_min = (quote.amount_out_raw * (10_000 - slippage)) // 10_000
        if amount_out_min <= 0:
            return self._ar(error="slippage bound collapses the minimum output to zero")

        signer = wallet.operational_signer()
        gate = wallet.policy

        # An allowance is required before the router can pull token_in. Refuse
        # with the exact remedy rather than broadcasting a call that reverts and
        # burns gas.
        allowance = univ3.read_allowance(params.chain, token_in, signer.address,
                                         quote.router)
        if allowance is not None and allowance < amount_in_raw:
            return self._ar(error=(
                f"insufficient allowance: the router may pull {allowance} but this "
                f"swap needs {amount_in_raw}. Call approve_token(token="
                f"{token_in}, spender={quote.router}, amount={params.amount_in}) "
                f"first, then swap, then revoke_approval."))

        idem = (f"defi_swap:{params.chain}:{token_in}:{token_out}:"
                f"{amount_in_raw}:{uuid.uuid4().hex[:8]}")
        data = univ3.build_exact_input_single_data(
            token_in=token_in, token_out=token_out, fee=quote.fee_tier,
            recipient=signer.address, amount_in_raw=amount_in_raw,
            amount_out_min_raw=amount_out_min)
        rail = (self._rail_factory or EvmRail)(chain=params.chain, signer=signer)
        try:
            tx = rail.build_call(to=quote.router, data=data, value=0)
        except Exception as exc:
            return self._ar(error=f"could not build the transaction: {exc}")

        # The swap spends token_in and grants nothing; any allowance the
        # simulation reveals is undeclared and the guard refuses it. The
        # router pair is MEASURED (watch_spenders) so its read delta — a
        # decrease when the router pulls token_in — is judged as a decrease,
        # not mistaken for a grant by the event-log cross-check.
        intent = tx_guard.TxIntent(
            chain=params.chain, token=token_in, to=quote.router,
            amount_raw=amount_in_raw, max_spend_usd=params.max_spend_usd,
            expected_allowance_grants=(), watch_spenders=(quote.router,),
            idempotency_key=idem)

        out_human = quote.amount_out_raw / (10 ** id_out.decimals)
        min_human = amount_out_min / (10 ** id_out.decimals)
        sanity_verdict, sanity_note = self._route_sanity(params.chain, quote,
                                                         id_in, id_out)
        if sanity_verdict == "DISAGREES":
            # §1.2: a disagreeing route BLOCKS — it used to only narrate. A pool
            # price is a number anyone with capital can seed; when it disagrees
            # with an independent source, executing anyway is how a thin or
            # manipulated pool extracts value bounded only by amount_in.
            return self._ar(error=(
                f"refused: route check DISAGREES — {sanity_note}. The pool is "
                f"quoting a price the independent source does not support "
                f"(drift above {_ROUTE_DRIFT_MAX_PCT:.0f}%); a thin or "
                f"manipulated pool extracts value this way. Nothing was "
                f"broadcast."))
        header = (
            f"swap {params.amount_in} {id_in.symbol or token_in} -> "
            f"{id_out.symbol or token_out}\n"
            f"  route: Uniswap V3 fee tier {quote.fee_tier}\n"
            f"  quoted out: {out_human:.8f}  (min after {slippage}bps slippage: "
            f"{min_human:.8f})\n"
            f"  {sanity_note}\n")

        async with gate.reserve():
            return self._run_guarded(
                intent=intent, tx=tx, rail=rail, gate=gate, signer=signer,
                execution_context=execution_context, header=header,
                dry_run=params.dry_run, venue_action="swap", idem=idem,
                counterparty=quote.router)

    def _route_sanity(self, chain, quote, id_in, id_out) -> Tuple[str, str]:
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
            price_in = self._price(chain, quote.token_in)
            price_out = self._price(chain, quote.token_out)
            if not price_in or not price_out:
                return ("UNAVAILABLE",
                        "route check: UNAVAILABLE — no independent price for one "
                        "side; the quote is unverified")
            amount_in_human = quote.amount_in_raw / (10 ** id_in.decimals)
            amount_out_human = quote.amount_out_raw / (10 ** id_out.decimals)
            if amount_out_human <= 0:
                return ("UNAVAILABLE", "route check: UNAVAILABLE — zero output")
            # What this route actually charges per unit of token_out, in USD.
            route_price = (amount_in_human * price_in) / amount_out_human
            drift = abs(route_price - price_out) / price_out * 100.0
            verdict = "AGREES" if drift <= _ROUTE_DRIFT_MAX_PCT else "DISAGREES"
            return (verdict,
                    f"route check: {verdict} — route implies "
                    f"${route_price:,.8f}/{id_out.symbol or 'token'} vs independent "
                    f"${price_out:,.8f} ({drift:.2f}% drift)")
        except Exception:
            return ("UNAVAILABLE",
                    "route check: UNAVAILABLE — the quote is unverified")
