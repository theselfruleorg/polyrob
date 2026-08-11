"""defi_trade — the agent's on-chain money verbs (proposal 023 T3).

Currently one verb: `transfer`. It is the simplest irreversible action, which
makes it the right thing to prove the rail with before swap (T4) or arbitrary
`contract_write` (T5) are built on top.

Every path here goes: build → `tx_guard.authorize()` → broadcast → confirm →
record, with `PolicyGate.reserve()` held across the whole span so two concurrent
transfers cannot both clear a nearly-exhausted cap. The tool never decides
policy; it may not broadcast without a `Decision(allowed=True)`.

`dry_run` defaults to TRUE. Moving real funds requires the caller to say so.
"""
from __future__ import annotations  # safe: @BaseTool.action uses explicit param_model

import logging
import types
import uuid
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool

logger = logging.getLogger(__name__)

_NULL_CONFIG = types.SimpleNamespace()


class TransferParams(BaseModel):
    chain: str = Field("base", description="Chain id (only 'base' is supported)")
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


class DefiTradeTool(BaseTool):
    def __init__(self, name: str = "defi_trade", config=None, container=None, *,
                 wallet=None, rail_factory=None, guard_fn=None, price_fn=None):
        super().__init__(name=name, config=config if config is not None else _NULL_CONFIG,
                         container=container)
        self._wallet = wallet
        self._rail_factory = rail_factory
        self._guard_fn = guard_fn
        self._price_fn = price_fn

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
