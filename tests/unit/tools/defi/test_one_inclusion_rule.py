"""One rule for what a total counts (prod 2026-11-13: "the value is not stable").

The portfolio counted only a ``high`` price and the positions book also counted
``low``, so one token had a value in one total and none in the other. Both now
read ``tools.intel.price.counts_in_total`` and show the rest as an indicative sum.
"""
from core import open_positions
from tools.defi import positions_view
from tools.defi.data_holdings import _indicative_line
from tools.intel.price import counts_in_total

MEME = "0xb200000000000000000000ea8625786a776539fb"
THIN = "0xb2000000000000000000000ff4a547c891ab1b01"


def _px(price, conf):
    return type("P", (), {"price_usd": price, "confidence": conf})()


def _entry(addr, sym, qty):
    return open_positions.PositionEntry(chain="base", address=addr, symbol=sym, qty=qty,
                                        entry_usd=1.0, entry_ts=1.0)


def test_only_a_high_price_counts():
    assert counts_in_total("high")
    assert not any(counts_in_total(c) for c in ("low", "disputed", "unknown", None))


def test_the_positions_total_counts_high_only_and_names_the_rest():
    prices = {MEME: _px(2.0, "high"), THIN: _px(3.0, "low")}
    text, _ = positions_view.build([_entry(MEME, "MEME", 10), _entry(THIN, "THIN", 10)],
                                   [], lambda c, a: prices[a], chain="base")
    assert "total value $20.00 over 1 position(s) at a high-confidence price" in text
    assert "NOT in the total: 1 position(s) at a low-confidence price, indicative ≈ $30.00" in text


def test_the_portfolio_names_what_its_total_leaves_out():
    assert _indicative_line([]) == []
    (line,) = _indicative_line([10.0, 2.5])
    assert line.startswith("NOT in the total: 2 holding(s)") and "$12.50" in line
