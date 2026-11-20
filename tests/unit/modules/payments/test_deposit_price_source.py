"""Proposal 125 S5: deposit credits value ETH at the ONE graded spend price.

The deposit monitor sits below ``tools/`` and never picks a price source: the
composition root injects ``price_fn`` (``price_sources.spend_price`` on mainnet
WETH). No price (``None``, non-finite, nothing wired) credits nothing.
"""
import ast
import math
import pathlib
import sys

import pytest

from modules.payments.deposit_monitor import DepositMonitor
from tests.unit.modules.payments.test_deposit_monitor_notify import (
    _Config, _FakeBalanceManager, _FakeDB, _fake_web3_module)

ROOT = pathlib.Path(__file__).resolve().parents[4]
_CHAIN = {"rpc_url": "http://fake-rpc.invalid", "chain_id": 1}
_TWO_ETH = 2 * 10**18


async def _tick(monitor, user="user_eth"):
    for d in await monitor._check_chain_deposits("0xabc", "ethereum", _CHAIN):
        await monitor._process_deposit(user, d)


@pytest.mark.asyncio
async def test_the_injected_price_values_eth(monkeypatch):
    monkeypatch.setitem(sys.modules, "web3", _fake_web3_module(_TWO_ETH))
    balances = _FakeBalanceManager()
    monitor = DepositMonitor(_FakeDB(), balances, _Config(), price_fn=lambda: 2500.0)
    readings = await monitor._check_chain_deposits("0xabc", "ethereum", _CHAIN)
    eth = next(d for d in readings if d["token_symbol"] == "ETH")
    assert float(eth["amount_usd"]) == pytest.approx(5000.0)
    await _tick(monitor)
    assert len(balances.add_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("price_fn", [None, lambda: None, lambda: math.nan,
                                      lambda: math.inf, lambda: 0.0, lambda: -1.0])
async def test_no_price_credits_nothing_and_keeps_the_reading(monkeypatch, price_fn):
    monkeypatch.setitem(sys.modules, "web3", _fake_web3_module(_TWO_ETH))
    balances = _FakeBalanceManager()
    monitor = DepositMonitor(_FakeDB(), balances, _Config(), price_fn=price_fn)
    readings = await monitor._check_chain_deposits("0xabc", "ethereum", _CHAIN)
    eth = next(d for d in readings if d["token_symbol"] == "ETH")
    # The price-independent reading survives (a balance reader still sees it)…
    assert eth["amount"] == str(_TWO_ETH)
    assert eth["amount_usd"] is None
    # …but nothing is credited this cycle.
    await _tick(monitor)
    assert balances.add_calls == []


@pytest.mark.asyncio
async def test_an_unpriced_tick_credits_on_the_next_priced_tick(monkeypatch):
    monkeypatch.setitem(sys.modules, "web3", _fake_web3_module(_TWO_ETH))
    balances = _FakeBalanceManager()
    prices = iter([None, 3000.0])
    monitor = DepositMonitor(_FakeDB(), balances, _Config(), price_fn=lambda: next(prices))
    await _tick(monitor)
    assert balances.add_calls == []
    await _tick(monitor)
    assert len(balances.add_calls) == 1


def test_the_composition_root_wires_the_one_spend_price():
    src = (ROOT / "polyrob/app/initialization.py").read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "DepositMonitor"]
    assert calls, "DepositMonitor is not constructed in the composition root"
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "price_fn" in kw, "DepositMonitor gets no price_fn"
    wired = ast.unparse(kw["price_fn"])
    assert "spend_price" in wired
    assert "price_oracle" not in src


def test_the_second_price_source_is_gone():
    assert not (ROOT / "modules/payments/price_oracle.py").exists()
    src = (ROOT / "modules/payments/deposit_monitor.py").read_text()
    assert "coingecko" not in src.lower()
    assert "ETH_PRICE_USD" not in src
