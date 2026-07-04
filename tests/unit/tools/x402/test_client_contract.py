import pytest
from core.wallet.signer import LocalEoaSigner
from tools.x402.client import FakeX402Client, X402Result

KEY = bytes.fromhex("11" * 32)


@pytest.mark.asyncio
async def test_fake_quote_returns_price():
    c = FakeX402Client(price_usd=0.25, pay_to="0xResource", paid_body="DATA")
    assert await c.quote("http://paid") == 0.25


@pytest.mark.asyncio
async def test_fake_fetch_returns_paid_result():
    c = FakeX402Client(price_usd=0.25, pay_to="0xResource", paid_body="DATA")
    res = await c.fetch_with_payment(
        url="http://paid", method="GET", body=None,
        signer=LocalEoaSigner(KEY), network="base-sepolia",
        facilitator_url="http://f", max_amount_usd=1.0,
    )
    assert isinstance(res, X402Result)
    assert res.paid is True and res.body == "DATA"
    assert res.amount_usd == 0.25 and res.pay_to == "0xResource"
