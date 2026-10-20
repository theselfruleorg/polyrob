"""079 W1 — the native coin is money, not "gas, not a position".

On 2026-10-12 the holdings read rendered 0.14 ETH as ``gas (ETH) … Not a position
and not counted in the total``, and the agent told the owner the money was "not
there". These tests pin the fix: the native row carries its USD value, it is
counted in ``total incl. native``, only the chain's ONE fee reserve
(``chains.fee_reserve``) is kept back, and an unpriced native is named, never
invented.
"""
import inspect
from decimal import Decimal

import pytest

from core.wallet import chains
from core.wallet.solana_onchain import SplHoldings
from tools.defi import data_holdings
from tools.defi.data_tool import DefiDataTool

EVM_WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
SOL_WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def _price(table):
    """price_fn answering ``high`` for the addresses in *table* only."""
    def fn(chain, address):
        usd = table.get(str(address).lower())
        return type("P", (), {"price_usd": usd,
                              "confidence": "high" if usd is not None else "unknown",
                              "pool_count": 3, "liquidity_usd": 1e6})()
    return fn


def _ident(c, a):
    return type("I", (), {"symbol": None, "name": None, "decimals": None,
                          "verified": False, "metadata_changed": False})()


def _evm(chain, native, *, priced=True):
    weth = chains.get(chain).wrapped_native.lower()
    tool = DefiDataTool(identity_fn=_ident,
                        price_fn=_price({weth: 2500.0} if priced else {}),
                        index_fn=lambda h, chain: {},
                        native_fn=lambda h, c: native)
    res = tool._evm_holdings_report(EVM_WALLET, chain, own=False)
    return res.extracted_content or "", res.metadata


def _sol(native, *, priced=True):
    wsol = chains.get("solana").wrapped_native.lower()
    tool = DefiDataTool(identity_fn=_ident,
                        price_fn=_price({wsol: 150.0} if priced else {}),
                        solana_holdings_fn=lambda h: SplHoldings())
    tool._solana_native = lambda h: native
    res = tool._solana_holdings_report(SOL_WALLET, "solana", own=False)
    return res.extracted_content or "", res.metadata


# -- the one fee reserve -------------------------------------------------------

def test_fee_reserve_values_per_d1():
    assert chains.fee_reserve("base") == Decimal("0.002")
    assert chains.fee_reserve("robinhood") == Decimal("0.002")
    assert chains.fee_reserve("arbitrum") == Decimal("0.002")
    assert chains.fee_reserve("optimism") == Decimal("0.002")
    assert chains.fee_reserve("ethereum") == Decimal("0.01")
    assert chains.fee_reserve("polygon") == Decimal("2")
    assert chains.fee_reserve("solana") == Decimal("0.02")
    assert chains.fee_reserve("nope") is None


def test_every_chain_row_sets_a_fee_reserve_in_coin_units():
    for row in chains.all_rows():
        assert isinstance(row.fee_reserve, Decimal) and row.fee_reserve > 0, row.name


# -- EVM -----------------------------------------------------------------------

def test_evm_native_row_is_valued_and_counted():
    text, md = _evm("base", 0.140662)
    assert ("  ETH 0.140662 ($351.66) — 0.138662 spendable above a 0.002 ETH "
            "fee reserve.") in text
    assert "total incl. native: $351.66 (native $351.66 + tokens $0.00)" in text
    assert "  tokens (confidently valued only): $0.00" in text
    assert md["native_usd"] == pytest.approx(351.655)


def test_robinhood_native_is_labelled_buying_power():
    text, _ = _evm("robinhood", 0.140662)
    assert "  ETH (gas + buying power) 0.140662 ($351.66)" in text


def test_unpriced_native_is_named_and_not_counted():
    text, md = _evm("base", 0.5, priced=False)
    assert "ETH 0.500000 (unpriced — not counted in the total)" in text
    assert "total incl. native: $0.00 — ETH unpriced, not counted" in text
    assert md["native_usd"] is None


def test_below_reserve_keeps_the_gas_tank_warning():
    text, _ = _evm("base", 0.001)
    assert "BELOW the 0.002 ETH fee reserve" in text and "gas tank" in text


def test_zero_native_is_an_empty_gas_tank():
    text, _ = _evm("base", 0.0)
    assert "the gas tank is EMPTY" in text


# -- Solana --------------------------------------------------------------------

def test_solana_native_row_is_valued_and_counted():
    text, md = _sol(0.5)
    assert ("  SOL (gas + buying power) 0.500000000 ($75.00) — 0.480000000 "
            "spendable above a 0.02 SOL fee reserve.") in text
    assert "total incl. native: $75.00 (native $75.00 + tokens $0.00)" in text
    assert md["native_usd"] == pytest.approx(75.0)


# -- ratchet -------------------------------------------------------------------

@pytest.mark.parametrize("native", [None, 0.0, 0.001, 0.5])
@pytest.mark.parametrize("priced", [True, False])
def test_no_native_row_says_not_a_position(native, priced):
    for text, _ in (_evm("base", native, priced=priced), _sol(native, priced=priced)):
        assert "Not a position" not in text


def test_holdings_source_never_calls_native_not_a_position():
    assert "Not a position" not in inspect.getsource(data_holdings)
