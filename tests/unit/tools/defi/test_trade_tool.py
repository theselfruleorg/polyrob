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
async def test_owner_queue_refusal_says_nothing_was_queued():
    """O5/O6: an owner_queue refusal is not a staged request — the text says so."""
    tool, gate = _tool(Decision(False, "owner approval required", "owner_queue", 50.0))
    res = await tool.transfer(_p(dry_run=True))
    assert "nothing was queued" in res.extracted_content
    assert "no approval request exists" in res.extracted_content
    tool, gate = _tool(Decision(False, "refused", "refuse", 50.0))
    res = await tool.transfer(_p(dry_run=True))
    assert "NOT SENT" in res.extracted_content
    assert "nothing was queued" not in res.extracted_content


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


# -- native gas-asset sends (2026-09-26: the owner asked for 0.9976 ETH to be
# sent and the verb had no way to say "ETH" — 'native' was not an address, the
# 0xEeee sentinel reports no decimals, and `call` with empty calldata says "that
# is transfer"). The guard has asserted native sends since 039 B1; the verb now
# declares one with `token=None`.

class _NativeRail(_Rail):
    def build_native_transfer(self, *, to, amount_wei):
        self.built = {"to": to, "data": "0x", "value": amount_wei, "chainId": 1}
        return self.built

    def build_erc20_transfer(self, **kw):  # pragma: no cover - must not be used
        raise AssertionError("a native send must not build an ERC-20 transfer")


def _native_tool(decision, *, receipt="success"):
    _Rail.receipt_status = receipt
    gate = _Gate()
    seen = {}

    def _guard(intent, tx, **kw):
        seen["intent"] = intent
        seen["tx"] = tx
        return decision

    return DefiTradeTool(wallet=_Wallet(gate), rail_factory=_NativeRail,
                         guard_fn=_guard, price_fn=lambda c, a: 1.0), gate, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["native", "NATIVE", "ETH", "eth",
                                   "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"])
async def test_native_send_declares_token_none_and_builds_a_value_transfer(token):
    tool, gate, seen = _native_tool(Decision(True, "authorized", "autonomous", 2700.0))
    res = await tool.transfer(_p(chain="ethereum", token=token, amount=0.997596,
                                 max_spend_usd=2800.0, dry_run=False))
    assert not res.error, res.error
    assert seen["intent"].token is None, "a native send is declared with token=None"
    assert seen["intent"].amount_raw == 997596000000000000
    assert seen["tx"]["value"] == 997596000000000000
    assert seen["tx"]["data"] == "0x"
    assert "SENT AND CONFIRMED" in res.extracted_content
    assert "ETH" in res.extracted_content
    assert gate.recorded and gate.recorded[0]["action"] == "transfer"


@pytest.mark.asyncio
async def test_native_send_dry_run_broadcasts_nothing():
    tool, gate, _ = _native_tool(Decision(True, "authorized", "autonomous", 1.0))
    res = await tool.transfer(_p(chain="ethereum", token="native"))
    assert "DRY RUN" in res.extracted_content
    assert _Rail.last.sent is False
    assert gate.recorded == []


@pytest.mark.asyncio
async def test_other_chain_gas_symbol_is_not_native_on_ethereum():
    """'POL' is polygon's gas asset; on ethereum it is a ticker, and a ticker
    is still refused."""
    tool, _, _ = _native_tool(Decision(True, "authorized", "autonomous", 1.0))
    res = await tool.transfer(_p(chain="ethereum", token="POL"))
    assert res.error


# Prod 2026-10-04 17:46: address-poisoning dust from lookalikes of the owner's
# payees. A transfer to a lookalike of a recent payee is refused before the
# guard runs, even when the guard would authorize it.

@pytest.mark.asyncio
async def test_a_lookalike_of_a_recent_payee_is_refused(monkeypatch):
    from core.wallet import address_lookalike
    monkeypatch.setattr(address_lookalike, "paid_counterparties",
                        lambda **kw: ["0x45Dd976d6E2f4557f2dfcD78FB75Bb19DCC32DC0"])
    _Rail.last = None
    tool, gate = _tool(Decision(True, "authorized", "owner_direct", 60.0))
    res = await tool.transfer(_p(to="0x45d4dccbe859ed59ba3a46de6eb3ec214f7c2dc0",
                                 dry_run=False))
    assert res.error and "POISONING" in res.error
    assert _Rail.last is None or _Rail.last.sent is False
    assert gate.recorded == []


@pytest.mark.asyncio
async def test_the_real_recent_payee_still_goes_through(monkeypatch):
    from core.wallet import address_lookalike
    monkeypatch.setattr(address_lookalike, "paid_counterparties",
                        lambda **kw: ["0x45Dd976d6E2f4557f2dfcD78FB75Bb19DCC32DC0"])
    tool, gate = _tool(Decision(True, "authorized", "owner_direct", 60.0))
    res = await tool.transfer(_p(to="0x45Dd976d6E2f4557f2dfcD78FB75Bb19DCC32DC0",
                                 dry_run=False))
    assert res.error is None and "SENT AND CONFIRMED" in res.extracted_content
