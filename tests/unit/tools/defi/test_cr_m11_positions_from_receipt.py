"""Confirmed swaps use preview balance changes; hostile events cannot size positions.
The preview remains an estimate and cannot be presented as a measured final fill.
"""
import types

import pytest
from unittest.mock import AsyncMock

from core.wallet.broadcast.evm import Receipt
from core.wallet.simulation import _TOPIC_TRANSFER
from tests.unit.tools.defi import test_trade_t4 as t4
from tools.defi.trade_tool import SwapParams

MEME = "0x" + "9" * 40
HOLDER = t4._Signer.address


@pytest.fixture(autouse=True)
def _clean_buy_screen(monkeypatch):
    # These cases exercise receipt accounting after a successful safety screen.
    monkeypatch.setattr("tools.defi.buy_screen.evm_buy_refusal", AsyncMock(return_value=None))


def _word(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def _transfer(token, frm, to, value):
    return {"address": token, "topics": [_TOPIC_TRANSFER, _word(frm), _word(to)],
            "data": "0x" + f"{value:064x}"}


def _rail(status, logs):
    class R(t4._Rail):
        def await_receipt(self, tx_hash, **kw):
            return Receipt(tx_hash=tx_hash, status=status, block_number=1)

        def _rpc(self, method, params, *a, **k):
            assert method == "eth_getTransactionReceipt"
            return None if logs is None else {"logs": logs}
    return R


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import tools.defi.providers.univ3 as u
    from core.wallet import tokens
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    real = tokens.get_token_identity

    def ident(chain, token):
        if token.lower() == MEME:
            return types.SimpleNamespace(decimals=18, symbol="MEME")
        return real(chain, token)
    monkeypatch.setattr(tokens, "get_token_identity", ident)
    yield
    t4._Rail.last = None


async def _swap(status, logs, *, out_raw=3 * 10**17):
    tool, gate = t4._tool(quote=t4._quote())
    tool._rail_factory = _rail(status, logs)
    from dataclasses import replace
    original_guard = tool._guard_fn
    tool._guard_fn = lambda *a, **kw: replace(original_guard(*a, **kw),
        simulated_token_deltas={t4.USDC: -1_000_000, MEME: out_raw})
    res = await tool.swap(SwapParams(token_in=t4.USDC, token_out=MEME,
                                     amount_in=1.0, max_spend_usd=2.0,
                                     dry_run=False))
    assert res.error is None, res.error
    return gate.recorded[0]


LANDED = [_transfer(t4.USDC, HOLDER, "0x" + "a" * 40, 1_000_000),
          _transfer(MEME, "0x" + "a" * 40, HOLDER, 3 * 10 ** 17)]


@pytest.mark.asyncio
async def test_a_reverted_swap_writes_no_position():
    rec = await _swap("failed", LANDED)
    assert not rec["positions"]


@pytest.mark.asyncio
async def test_an_unconfirmed_swap_writes_no_position():
    rec = await _swap("pending", LANDED)
    assert not rec["positions"]


@pytest.mark.asyncio
async def test_a_confirmed_swap_ignores_inflated_receipt_events():
    forged = LANDED + [_transfer(MEME, "0x" + "a" * 40, HOLDER, 10**30)]
    rec = await _swap("success", forged)
    legs = {p.address.lower(): p for p in rec["positions"]}
    assert MEME in legs
    assert legs[MEME].qty == pytest.approx(0.3)
    assert legs[MEME].qty_source == "simulation"


@pytest.mark.asyncio
async def test_a_succeeded_swap_with_no_arrival_writes_no_position():
    rec = await _swap("success", LANDED, out_raw=0)
    assert not rec["positions"]


@pytest.mark.asyncio
async def test_missing_event_logs_do_not_erase_the_labeled_preview():
    rec = await _swap("success", None)
    assert rec["positions"] and all(p.qty_source == "simulation" for p in rec["positions"])
