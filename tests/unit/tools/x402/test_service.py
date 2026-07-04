import pytest
from core.wallet.config import WalletConfig
from core.wallet.agent_wallet import AgentWallet
from tools.x402.client import FakeX402Client
from tools.x402.service import X402PayTool, FetchParams, QuoteParams


def _wallet():
    cfg = WalletConfig(enabled=True, backend="local_eoa", master_seed="s" * 40,
                       network="testnet", max_per_tx_usd=10.0,
                       x402_client_enabled=True, x402_facilitator_url="http://f")
    return AgentWallet(cfg)


def _tool(client):
    return X402PayTool(wallet=_wallet(), client=client)


@pytest.mark.asyncio
async def test_quote_reports_price():
    tool = _tool(FakeX402Client(price_usd=0.25, pay_to="0xR", paid_body="X"))
    res = await tool.x402_quote(QuoteParams(url="http://paid"))
    assert "0.25" in res.extracted_content


@pytest.mark.asyncio
async def test_fetch_pays_and_returns_body():
    tool = _tool(FakeX402Client(price_usd=0.25, pay_to="0xR", paid_body="SECRET-DATA"))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert "SECRET-DATA" in res.extracted_content
    assert res.error is None


@pytest.mark.asyncio
async def test_fetch_rejects_over_cap():
    tool = _tool(FakeX402Client(price_usd=5.0, pay_to="0xR", paid_body="X"))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert res.error is not None and "exceeds" in res.error.lower()


@pytest.mark.asyncio
async def test_fetch_rejects_over_catastrophic_ceiling():
    # wallet ceiling is 10.0; ask price 50 but cap high → PolicyGate blocks
    tool = _tool(FakeX402Client(price_usd=50.0, pay_to="0xR", paid_body="X"))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=100.0))
    assert res.error is not None and "ceiling" in res.error.lower()


@pytest.mark.asyncio
async def test_result_never_contains_private_key():
    w = _wallet()
    raw = w._derive_key("x402").hex()
    tool = X402PayTool(wallet=w, client=FakeX402Client(price_usd=0.1, pay_to="0xR", paid_body="X"))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert raw not in (res.extracted_content or "").lower()


@pytest.mark.asyncio
async def test_wallet_status_reports_addresses_not_keys():
    w = _wallet()
    raw = w._derive_key("x402").hex()
    tool = X402PayTool(wallet=w, client=FakeX402Client(price_usd=None, pay_to=None, paid_body="X"))
    from tools.x402.service import EmptyWalletParams
    res = await tool.x402_wallet_status(EmptyWalletParams())
    assert w.signer_for("x402").address in res.extracted_content
    assert raw not in res.extracted_content.lower()


@pytest.mark.asyncio
async def test_disabled_wallet_errors_cleanly():
    tool = X402PayTool(wallet=None, client=FakeX402Client(price_usd=0.1, pay_to="0xR", paid_body="X"))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert res.error is not None and "not enabled" in res.error.lower()
