"""A real sub-cent price must not render as $0.00.

Found live while verifying the holder work: `token_info` on a live Robinhood
token printed `price: $0.00   confidence: high`. The price was real and the
confidence was earned; only the formatter was wrong. A memecoin trades at
1e-5 to 1e-8 almost by definition, so the default two-decimal money format
turns nearly every token this path exists to assess into a zero.

The agent had already hit it and filed it as a token property: its own screen
log records "price feed shows $0.00 (edge case)" against HARMONIC, and the
verdict written from that reads as if the token had no price.
"""
from tools.defi.data_tool import _fmt_usd


def test_a_sub_cent_price_keeps_its_digits():
    assert _fmt_usd(0.00003011) == "$0.00003011"


def test_a_very_small_price_keeps_its_digits():
    assert _fmt_usd(4.35e-7) == "$0.000000435"


def test_a_negative_sub_cent_value_keeps_its_digits():
    assert _fmt_usd(-0.00003011) == "-$0.00003011"


def test_ordinary_money_is_unchanged():
    assert _fmt_usd(2_230_000.0) == "$2,230,000.00"
    assert _fmt_usd(1.5) == "$1.50"
    assert _fmt_usd(0.25) == "$0.25"
    assert _fmt_usd(0.01) == "$0.01"


def test_a_true_zero_is_still_zero():
    assert _fmt_usd(0.0) == "$0.00"


def test_unknown_is_still_the_word():
    assert _fmt_usd(None) == "unknown"
