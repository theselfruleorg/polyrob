"""The uniqueness nudge lands on an amount a human can actually send.

⚠️ Live, Playground Env 2026-09-15. A 3,500 PNL ban offer quoted

    SEND EXACTLY 3500.000000000000000005 PNL

The nudge steps by ONE raw unit. On 6-decimal USDC that is a millionth of a
dollar — invisible and harmless. On an 18-decimal token it is 5e-18, a figure
no wallet renders and no person can type, while settlement matches the integer
EXACTLY. The payer is asked for a number they cannot reproduce.

The step must scale with the asset so the nudge is VISIBLE and ROUND: whole
tokens for a memecoin-scale asset, unchanged for a stablecoin.
"""
import pytest

from modules.x402.invoice_jitter import jitter_step


# --- the asset that broke ---------------------------------------------------

def test_an_18_decimal_token_steps_by_a_whole_token():
    """3500 -> 3501, not 3500.000000000000000001."""
    assert jitter_step(18) == 10 ** 18


def test_the_nudged_amount_stays_round():
    base = 3500 * 10 ** 18
    nudged = base + 3 * jitter_step(18)
    assert nudged % (10 ** 18) == 0, "the amount grew a fractional tail"
    assert nudged // 10 ** 18 == 3503


# --- the assets that must not change ---------------------------------------

@pytest.mark.parametrize("decimals", [0, 2, 6, 8])
def test_a_stablecoin_scale_asset_is_unchanged(decimals):
    """⚠️ USDC keeps its one-raw-unit step. Widening it would move a real
    price by a visible cent for no reason."""
    assert jitter_step(decimals) == 1


def test_the_boundary_is_explicit():
    """Whatever the cutoff is, it is a decision — pin it so a later edit is
    deliberate rather than incidental."""
    assert jitter_step(11) == 1
    assert jitter_step(12) == 10 ** 12


# --- honesty ---------------------------------------------------------------

def test_the_step_is_never_zero():
    """A zero step would loop forever looking for a free amount."""
    for d in range(0, 25):
        assert jitter_step(d) >= 1


def test_a_nonsense_decimals_value_degrades_to_one():
    for bad in (-1, None, "x"):
        assert jitter_step(bad) == 1
