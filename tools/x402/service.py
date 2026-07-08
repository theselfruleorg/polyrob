"""X402PayTool — the agent pays for paid resources during a job (gated OFF).

Consumes the AgentWallet x402 signer + an X402PaymentClient boundary. Enforces:
the per-call max_amount_usd cap, the wallet's catastrophic PolicyGate ceiling,
payTo-binding (pays only the resource it called), idempotency, and the
LLM-never-sees-key invariant (returns body/tx_hash/address only).
"""
from __future__ import annotations  # safe: @BaseTool.action uses explicit param_model

import logging
import types
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool

# Minimal stand-in used when no BotConfig is provided (e.g. in unit tests).
# The tool never reads config fields; it drives only through the injected
# wallet/client pair.  A real production instantiation always passes a config.
_NULL_CONFIG = types.SimpleNamespace()


class QuoteParams(BaseModel):
    url: str = Field(..., description="URL of a possibly-paid resource to price")


class FetchParams(BaseModel):
    url: str = Field(..., description="URL to fetch; pays via x402 if it returns 402")
    method: str = Field("GET", description="HTTP method")
    body: Optional[str] = Field(None, description="Optional request body")
    max_amount_usd: float = Field(..., gt=0, description="Max USD you authorize for this single fetch")


class EmptyWalletParams(BaseModel):
    pass


class X402PayTool(BaseTool):
    def __init__(self, name: str = "x402_pay", config=None, container=None, *, wallet=None, client=None):
        super().__init__(name=name, config=config if config is not None else _NULL_CONFIG, container=container)
        self._wallet = wallet
        self._client = client
        self._wallet_resolved = wallet is not None
        self._client_resolved = client is not None

    def _get_wallet(self):
        if not self._wallet_resolved:
            from core.wallet.factory import get_agent_wallet
            self._wallet = get_agent_wallet()
            self._wallet_resolved = True
        return self._wallet

    def _get_client(self):
        if not self._client_resolved:
            from tools.x402.real_client import RealX402Client
            self._client = RealX402Client()
            self._client_resolved = True
        return self._client

    def _ar(self, *, content: str = None, error: str = None):
        from tools.controller.types import ActionResult
        if error is not None:
            return ActionResult(error=error)
        return ActionResult(extracted_content=content)

    @BaseTool.action("Get the x402 price (USD) of a resource without paying", param_model=QuoteParams)
    async def x402_quote(self, params: QuoteParams, execution_context=None):
        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (set AGENT_WALLET_ENABLED=true)")
        try:
            price = await self._get_client().quote(params.url)
            if price is None:
                return self._ar(content=f"{params.url} is not a paid resource (no x402 challenge)")
            return self._ar(content=f"{params.url} requires x402 payment of ${price:.4f} USD")
        except Exception as e:
            logging.getLogger(__name__).error(f"x402_quote failed: {e}")
            return self._ar(error=f"x402_quote failed: {e}")

    @BaseTool.action("Fetch a resource, auto-paying via x402 up to max_amount_usd", param_model=FetchParams)
    async def x402_fetch(self, params: FetchParams, execution_context=None):
        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (set AGENT_WALLET_ENABLED=true)")
        cfg = wallet.config
        # Sign with the OPERATIONAL venue (default 'treasury') so payment draws from the
        # funded address (== wallet.address). The policy venue label below stays "x402"
        # for per-venue caps/accounting. See core/wallet/agent_wallet.py::operational_signer.
        signer = wallet.operational_signer()
        # One idempotency key per fetch, reused by both check() and record() so a
        # retried step's check() finds the prior record() (else replay-protection
        # fails to correlate and could double-pay).
        idem = None
        # Pre-flight price + policy
        try:
            price = await self._get_client().quote(params.url)
        except Exception as e:
            return self._ar(error=f"x402 quote failed: {e}")
        if price is not None:
            if price > params.max_amount_usd:
                return self._ar(error=f"price ${price:.4f} exceeds your max_amount_usd ${params.max_amount_usd:.2f}")
            idem = f"x402:{params.url}:{price}"
            decision = wallet.policy.check(venue="x402", amount_usd=price, idempotency_key=idem)
            if not decision.allowed:
                return self._ar(error=f"payment blocked: {decision.reason}")
        try:
            res = await self._get_client().fetch_with_payment(
                url=params.url, method=params.method, body=params.body,
                signer=signer, network=cfg.network, facilitator_url=cfg.x402_facilitator_url,
                max_amount_usd=params.max_amount_usd,
            )
        except Exception as e:
            logging.getLogger(__name__).error(f"x402_fetch failed: {e}")
            return self._ar(error=f"x402_fetch failed: {e}")
        if res.paid:
            record_key = idem if idem is not None else f"x402:{params.url}:{res.amount_usd}"
            wallet.policy.record(venue="x402", action="pay", amount_usd=res.amount_usd,
                                 counterparty=res.pay_to, idempotency_key=record_key,
                                 result_ref=res.tx_hash)
            header = f"[paid ${res.amount_usd:.4f} to {res.pay_to}, tx {res.tx_hash}]\n"
        else:
            header = ""
        return self._ar(content=f"{header}{res.body}")

    @BaseTool.action("Show the agent wallet x402 address and recent payment audit", param_model=EmptyWalletParams)
    async def x402_wallet_status(self, params: EmptyWalletParams, execution_context=None):
        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (set AGENT_WALLET_ENABLED=true)")
        addr = wallet.operational_signer().address
        audit = wallet.policy.audit_log
        spent = sum(e["amount_usd"] for e in audit if e["venue"] == "x402")
        return self._ar(content=f"x402 pays from ({wallet.operational_venue}) address: {addr}\nx402 payments this process: {len(audit)} (≈${spent:.4f})")
