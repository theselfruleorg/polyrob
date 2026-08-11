"""defi_trade.transfer — dry-run default, refusal honesty, receipt honesty."""
import contextlib
import types

import pytest

from core.wallet.tx_guard import Decision
from tools.defi.trade_tool import DefiTradeTool, TransferParams

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


class _Gate:
    def __init__(self):
        self.recorded = []

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def record(self, **kw):
        self.recorded.append(kw)


class _Signer:
    address = "0x2222222222222222222222222222222222222222"


class _Wallet:
    def __init__(self, gate):
        self.policy = gate

    def operational_signer(self):
        return _Signer()


class _Rail:
    """Records what it was asked to do; broadcasts nothing unless told."""
    last = None

    def __init__(self, chain, signer, **kw):
        self.chain = chain
        self.sent = False
        _Rail.last = self

    def build_erc20_transfer(self, *, token, to, amount_raw):
        self.built = {"to": token, "data": "0xa9059cbb", "value": 0,
                      "chainId": 8453, "amount_raw": amount_raw}
        return self.built

    def sign_and_send(self, tx):
        self.sent = True
        return "0x" + "ab" * 32

    def await_receipt(self, tx_hash, **kw):
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status=self.receipt_status,
                       block_number=99, gas_used=52000)

    receipt_status = "success"


def _tool(decision, *, receipt="success"):
    _Rail.receipt_status = receipt
    gate = _Gate()
    return DefiTradeTool(
        wallet=_Wallet(gate),
        rail_factory=_Rail,
        guard_fn=lambda intent, tx, **kw: decision,
        price_fn=lambda c, a: 1.0,
    ), gate


def _p(**kw):
    base = dict(chain="base", token=USDC, to=TO, amount=0.25, max_spend_usd=0.30)
    base.update(kw)
    return TransferParams(**base)


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_broadcasts_nothing():
    tool, gate = _tool(Decision(True, "authorized", "autonomous", 0.25))
    res = await tool.transfer(_p())
    assert "DRY RUN" in res.extracted_content
    assert _Rail.last.sent is False
    assert gate.recorded == [], "a dry run must not touch the spend ledger"


@pytest.mark.asyncio
async def test_a_refusal_says_nothing_was_broadcast():
    tool, gate = _tool(Decision(False, "refused: cap exceeded", "refuse", 5.0))
    res = await tool.transfer(_p(dry_run=False))
    assert "NOT SENT" in res.extracted_content
    assert _Rail.last.sent is False
    assert gate.recorded == []


@pytest.mark.asyncio
async def test_owner_queue_lane_does_not_execute():
    tool, gate = _tool(Decision(False, "owner approval required", "owner_queue", 50.0))
    res = await tool.transfer(_p(dry_run=False))
    assert "owner_queue" in res.extracted_content
    assert _Rail.last.sent is False


@pytest.mark.asyncio
async def test_authorized_non_dry_run_broadcasts_and_records():
    tool, gate = _tool(Decision(True, "authorized", "autonomous", 0.25))
    res = await tool.transfer(_p(dry_run=False))
    assert "SENT AND CONFIRMED" in res.extracted_content
    assert _Rail.last.sent is True
    assert len(gate.recorded) == 1
    assert gate.recorded[0]["venue"] == "defi"
    assert gate.recorded[0]["amount_usd"] == 0.25


@pytest.mark.asyncio
async def test_a_reverted_receipt_is_reported_as_not_transferred():
    tool, gate = _tool(Decision(True, "authorized", "autonomous", 0.25),
                       receipt="failed")
    res = await tool.transfer(_p(dry_run=False))
    assert "REVERTED" in res.extracted_content
    assert "did NOT happen" in res.extracted_content


@pytest.mark.asyncio
async def test_pending_receipt_warns_against_blind_retry():
    tool, gate = _tool(Decision(True, "authorized", "autonomous", 0.25),
                       receipt="pending")
    res = await tool.transfer(_p(dry_run=False))
    assert "NOT CONFIRMED" in res.extracted_content
    assert "do NOT retry" in res.extracted_content


@pytest.mark.asyncio
async def test_a_ticker_instead_of_an_address_is_refused():
    tool, _ = _tool(Decision(True, "authorized", "autonomous", 0.25))
    res = await tool.transfer(_p(token="USDC"))
    assert res.error


@pytest.mark.asyncio
async def test_spend_is_recorded_even_on_a_reverted_tx():
    """Gas was spent and a nonce consumed — the ledger must not pretend nothing
    happened."""
    tool, gate = _tool(Decision(True, "authorized", "autonomous", 0.25),
                       receipt="failed")
    await tool.transfer(_p(dry_run=False))
    assert len(gate.recorded) == 1
