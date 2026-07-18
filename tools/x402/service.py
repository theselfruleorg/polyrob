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
            # C1 half 2 (2026-07-15): thread the wallet's PolicyGate into the
            # client so the paying leg can re-check the SDK-selected
            # requirement's REAL amount against the catastrophic ceiling / daily
            # cap before any payload is signed. The service-layer check (half 1)
            # authorizes at the worst case (max_amount_usd); this arms the
            # client-side gate on the ACTUAL amount, at sign time, with fresh
            # gate state. `_get_wallet()` is cached and is always resolved
            # non-None before a paying entry point runs (x402_fetch checks it
            # first); when the wallet is disabled, policy=None is harmless (no
            # fetch happens on that path).
            wallet = self._get_wallet()
            policy = wallet.policy if wallet is not None else None
            self._client = RealX402Client(policy=policy)
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
        # Owner kill-switch: refuse ALL spend while autonomy is halted (defence beyond caps).
        # G-11: fail CLOSED. An import/probe failure must never silently disable the
        # halt check on a money path — refuse the payment and name the failure.
        try:
            from core.config_policy import AutonomyConfig
            halted = AutonomyConfig.autonomy_halted()
        except Exception as e:
            return self._ar(error=f"payment refused: kill-switch probe failed ({e}); failing closed")
        if halted:
            return self._ar(error="payment refused: autonomy is HALTED (owner kill-switch)")
        cfg = wallet.config
        # Sign with the OPERATIONAL venue (default 'treasury') so payment draws from the
        # funded address (== wallet.address). The policy venue label below stays "x402"
        # for per-venue caps/accounting. See core/wallet/agent_wallet.py::operational_signer.
        signer = wallet.operational_signer()
        # One idempotency key per fetch, reused by both check() and record() so a
        # retried step's check() finds the prior record() (else replay-protection
        # fails to correlate and could double-pay).
        # Pre-flight price + policy. quote() is ADVISORY: RealX402Client returns
        # None on any error, a non-402 bare GET, or a method/body-specific paywall.
        # SECURITY (P0): the PolicyGate (per-tx ceiling, daily cap, per-venue cap,
        # idempotency replay-guard) must run UNCONDITIONALLY before fetch_with_payment.
        # When the probe can't price it, fail closed to the agent's authorized
        # ceiling (max_amount_usd) as the worst-case spend — never skip the gate.
        try:
            price = await self._get_client().quote(params.url)
        except Exception as e:
            return self._ar(error=f"x402 quote failed: {e}")
        if price is not None and price > params.max_amount_usd:
            return self._ar(error=f"price ${price:.4f} exceeds your max_amount_usd ${params.max_amount_usd:.2f}")
        # SECURITY (C1, 2026-07-15): authorize the gate at the WORST CASE the SDK can
        # pay — params.max_amount_usd — never the advisory quote. quote() is an
        # attacker-controllable, unauthenticated probe; the actual paying leg
        # (fetch_with_payment) is bounded only by max_amount_usd, so a low quote with a
        # high authorization would let the PolicyGate ceiling/daily cap be bypassed
        # (pay up to max_amount_usd while the gate only saw the cheap probe amount).
        # Checking at max_amount_usd means any real settlement <= it is within what the
        # gate authorized; record() below still logs the ACTUAL settled amount.
        check_amount = params.max_amount_usd
        # G-10: key on the AUTHORIZED ceiling (params.max_amount_usd), not on
        # check_amount (quote-price-or-ceiling). check_amount can differ between a
        # step and its retry purely because quote() availability changed (e.g. the
        # probe timed out the first time), which would mint a DIFFERENT key each
        # attempt and break the replay-guard's ability to correlate them — risking
        # a double-pay on retry. max_amount_usd is stable across retries by
        # construction (it's the agent's one-time authorization for this fetch).
        # Trade-off (deliberate): check() is NOT a reservation — a checked-but-
        # failed fetch must stay retryable, so we do not mark the key "seen" here;
        # only record() (after an actual payment) adds it to the replay-guard set.
        idem = f"x402:{params.url}:{params.max_amount_usd}"
        # M4 (2026-07-15): PolicyGate.check() -> (network pay leg) -> record() is
        # the value-moving critical section. Without holding the gate's reserve
        # lock across it, two concurrent x402_fetch calls can both check() a
        # nearly-exhausted cap at the same stale rolling-spend, both pass, then
        # both record() — clearing past the cap. `reserve()` serializes this
        # per-process (per PolicyGate instance); check()/record() themselves do
        # NOT acquire the lock, so the client's own before-payment re-check
        # (RealX402Client._abort_if_invalid_requirement -> policy.check(), fired
        # from inside the awaited fetch_with_payment below) cannot deadlock
        # against it.
        async with wallet.policy.reserve():
            decision = wallet.policy.check(venue="x402", amount_usd=check_amount, idempotency_key=idem)
            if not decision.allowed:
                return self._ar(error=f"payment blocked: {decision.reason}")
            try:
                res = await self._get_client().fetch_with_payment(
                    url=params.url, method=params.method, body=params.body,
                    signer=signer, network=cfg.network,
                    max_amount_usd=params.max_amount_usd,
                )
            except Exception as e:
                logging.getLogger(__name__).error(f"x402_fetch failed: {e}")
                return self._ar(error=f"x402_fetch failed: {e}")
            if res.paid:
                # Reuse the same idem key check() used so replay-protection correlates.
                wallet.policy.record(venue="x402", action="pay", amount_usd=res.amount_usd,
                                     counterparty=res.pay_to, idempotency_key=idem,
                                     result_ref=res.tx_hash)
                # Finding 2 (Task 4 review, cheap-related): amount_is_estimate was
                # captured on X402Result but never surfaced — the audit/user trail
                # couldn't tell a confirmed-settled figure from a pre-settlement
                # estimate. Mark it in the header so that distinction is visible.
                estimate_marker = " (estimated)" if res.amount_is_estimate else ""
                header = f"[paid ${res.amount_usd:.4f}{estimate_marker} to {res.pay_to}, tx {res.tx_hash}]\n"
            else:
                header = ""
        return self._ar(content=f"{header}{res.body}")

    @BaseTool.action("Show the agent wallet: address, ON-CHAIN USDC/gas balance, spend caps, and payment audit", param_model=EmptyWalletParams)
    async def x402_wallet_status(self, params: EmptyWalletParams, execution_context=None):
        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (set AGENT_WALLET_ENABLED=true)")
        venue = wallet.operational_venue
        addr = wallet.operational_signer().address
        cfg = wallet.config
        lines = [f"Agent wallet ({venue}) address: {addr}  [network={cfg.network}]"]
        # On-chain balance so the agent KNOWS what it can actually spend (fail-open).
        if cfg.network == "mainnet":
            try:
                from core.wallet.onchain import balances, venue_chain
                native, usdc = balances(addr, venue_chain(venue) or "base")
                u = f"${usdc:.2f}" if usdc is not None else "unavailable"
                g = f"{native:.5f}" if native is not None else "unavailable"
                lines.append(f"On-chain balance: {u} USDC | gas {g} ETH")
            except Exception:
                lines.append("On-chain balance: unavailable")
        else:
            lines.append("On-chain balance: (testnet — not shown)")
        daily = getattr(cfg, "daily_cap_usd", None)
        lines.append(f"Spend caps: max ${cfg.max_per_tx_usd:.2f}/tx"
                     + (f" · ${daily:.2f}/day" if daily is not None else ""))
        # G-12: count/sum the SAME filtered (venue == "x402") set the total is
        # labeled as. `audit` is the FULL cross-venue log, and with the
        # persistent JSONL sink (audit_sink.py) it also spans the whole lifetime
        # of the wallet, not just "this process" — len(audit) mislabeled both the
        # scope (all venues) and the window (all time) of the count.
        audit = wallet.policy.audit_log
        x402_entries = [e for e in audit if e["venue"] == "x402"]
        spent = sum(e["amount_usd"] for e in x402_entries)
        lines.append(f"x402 payments (all recorded): {len(x402_entries)} (≈${spent:.4f})")
        return self._ar(content="\n".join(lines))
