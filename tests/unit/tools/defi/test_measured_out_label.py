"""The settled swap notice reports what the RECEIPT paid, not only the quote.

Live 2026-09-22 (intel, the DELTA full exit): the per-leg CONFIRMED notice said
`quoted ≥0.00096881 WETH · not independently measured` while the rail four minutes
later reported the measured 0.00098357 WETH — a WEAKER honesty claim than the same
run could already support. `tx_notify.render_settled` was already correct (it prints
`measured X` when given one and the caveat when not); nothing was passing `measured`.

`_swap_sizes_from_receipt` already sums the token_out `Transfer` logs to the holder
for the positions classifier, so the number was in hand and being dropped. These tests
pin the label helper: the quantity comes from the receipt, the unit from the quote so
the two lines compare directly, and anything unmeasurable yields None so the notice
keeps its honest caveat rather than inventing a measurement.
"""
import pytest

from tools.defi.trade_tool import _measured_out_label


def test_receipt_quantity_with_the_quotes_unit():
    out = _measured_out_label({"out_qty": 0.00098357}, "≥0.00096881 WETH")
    assert out == "0.00098357 WETH"


def test_the_measurement_can_be_below_the_quoted_floor():
    """A min-out floor is a floor on the QUOTE; the receipt is whatever landed."""
    out = _measured_out_label({"out_qty": 0.00090000}, "≥0.00096881 WETH")
    assert out == "0.00090000 WETH"


def test_multiword_unit_is_kept_whole():
    out = _measured_out_label({"out_qty": 12.5}, "≥10 PONS GIRL")
    assert out == "12.50000000 PONS GIRL"


def test_no_quote_label_still_reports_the_quantity():
    assert _measured_out_label({"out_qty": 3.0}, None) == "3.00000000"


@pytest.mark.parametrize("sized", [None, {}, {"out_qty": None}, {"out_qty": "abc"},
                                   {"out_qty": 0}, {"out_qty": -1.0}, "not-a-dict"])
def test_unmeasurable_yields_none_not_a_zero(sized):
    """None keeps the notice's honest 'not independently measured' line.

    A `0.00000000 WETH` measurement would be a confident statement that nothing
    arrived, which is a different claim from 'we did not measure'.
    """
    assert _measured_out_label(sized, "≥1 WETH") is None


def test_render_settled_prefers_the_measurement_over_the_caveat():
    """End to end through the renderer the notice actually uses."""
    from core.wallet import tx_notify
    measured = tx_notify.render_settled(tx_notify.TxNotice(
        verb="swap", route="robinhood", tx_ref="0x" + "ab" * 32,
        amount_out="≥0.00096881 WETH", measured="0.00098357 WETH",
        state=tx_notify.STATE_CONFIRMED))
    assert "measured 0.00098357 WETH" in measured
    assert "not independently measured" not in measured

    unmeasured = tx_notify.render_settled(tx_notify.TxNotice(
        verb="swap", route="robinhood", tx_ref="0x" + "ab" * 32,
        amount_out="≥0.00096881 WETH", state=tx_notify.STATE_CONFIRMED))
    assert "not independently measured" in unmeasured
