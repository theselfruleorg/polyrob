"""x402_fetch never reports a payment as paid when the seller answered 202 "settling".

A seller whose settlement is not proven answers 202, serves nothing and says "do not pay
again" (``modules/x402/middleware.py``, ``api/x402_endpoints.py::pay_invoice``). The
payer signed an authorization that may still land, so the spend stays booked against the
caps — but the result header said "[paid $… tx None]" (found by the level-1 interface
rig, ``ops/rig``, scenario PX6).
"""
import pytest

from core.wallet.agent_wallet import AgentWallet
from core.wallet.config import WalletConfig
from tools.x402.client import X402Result
from tools.x402.service import FetchParams, X402PayTool


def _wallet():
    cfg = WalletConfig(enabled=True, backend="local_eoa", master_seed="s" * 40,
                       network="testnet", max_per_tx_usd=10.0,
                       x402_client_enabled=True, x402_facilitator_url="http://f")
    return AgentWallet(cfg)


class _Client:
    def __init__(self, status, tx):
        self.status, self.tx = status, tx

    async def quote(self, url, **kwargs):
        return None

    async def fetch_with_payment(self, *, max_amount_usd, **kwargs):
        return X402Result(body='{"status":"settling"}', paid=True, amount_usd=max_amount_usd,
                          tx_hash=self.tx, pay_to="0xR", status_code=self.status,
                          amount_is_estimate=True, authorized_amount_usd=max_amount_usd)


@pytest.mark.asyncio
async def test_a_202_settling_answer_is_unconfirmed_not_paid():
    wallet = _wallet()
    tool = X402PayTool(wallet=wallet, client=_Client(202, None))
    res = await tool.x402_fetch(FetchParams(url="http://paid/inv", method="POST",
                                            max_amount_usd=0.5, expected_pay_to="0xR"))
    text = res.extracted_content or ""
    assert res.error is None
    assert text.startswith("[PAYMENT UNCONFIRMED: $0.5000")
    assert "[paid" not in text and "Do NOT pay again" in text
    # the authorization may land: it stays booked against the caps
    assert [e["amount_usd"] for e in wallet.policy.audit_log if e["venue"] == "x402"] == [0.5]


@pytest.mark.asyncio
async def test_a_200_with_a_tx_still_reads_paid():
    tool = X402PayTool(wallet=_wallet(), client=_Client(200, "0x" + "ab" * 32))
    res = await tool.x402_fetch(FetchParams(url="http://paid/inv", method="POST",
                                            max_amount_usd=0.5, expected_pay_to="0xR"))
    assert (res.extracted_content or "").startswith("[paid $0.5000")
