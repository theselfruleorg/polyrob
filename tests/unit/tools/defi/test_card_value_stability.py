"""Card values that do not move on their own (prod 2026-11-13: "the value is not stable").

* A `$` bound is held to the value SENT (no network fee, no rent), the number
  the guard holds max_spend_usd to — never the fee-included simulated value.
* A `$` conversion never carries more places than the token has.
"""
from types import SimpleNamespace

from surfaces.telegram.send_ops import _simulated_usd
from tools.defi import send_order


def _quote(text):
    return SimpleNamespace(extracted_content=text)


def test_the_bound_reads_the_value_sent_not_the_fee_included_value():
    body = ("solana transfer 5 USDC -> X\n"
            "  simulated value: $5.3500\n"
            "  value sent: $5.0000 (without the network fee and rent)\n")
    assert _simulated_usd(_quote(body)) == 5.0
    # $5 * 1.05 + 0.01 = 5.26: the old fee-included read refused "the price moved".
    assert _simulated_usd(_quote(body)) <= send_order.implied_max(5.0)


def test_without_a_value_sent_line_the_simulated_value_is_read():
    assert _simulated_usd(_quote("  simulated value: $1,234.5000\n")) == 1234.5


def test_a_symbol_cannot_forge_the_value_sent_line():
    body = ("transfer 1 value sent: $0.01 -> X\n"
            "  simulated value: $9.0000\n"
            "  value sent: $8.0000 (without the network fee)\n")
    assert _simulated_usd(_quote(body)) == 8.0


def test_a_dollar_conversion_is_cut_to_the_token_decimals():
    order = {"usd": 5.0, "chain": "base", "token": "0x" + "ab" * 20, "max_usd": None}
    out, px, why = send_order.resolve_usd(order, lambda c, a: 95_000.0,
                                          decimals_fn=lambda c, t: 8)
    assert why is None and px == 95_000.0
    assert out["amount"] == 0.00005263          # not 0.0000526316 (10 places)
    assert out["amount"] * px <= 5.0            # cut DOWN: never above the dollars


def test_less_than_one_unit_is_refused_by_name():
    order = {"usd": 0.01, "chain": "base", "token": "0x" + "ab" * 20, "max_usd": None}
    out, _, why = send_order.resolve_usd(order, lambda c, a: 95_000.0,
                                         decimals_fn=lambda c, t: 2)
    assert out is None and "less than one unit" in why


def test_unknown_decimals_keep_six_significant_digits():
    assert send_order.round_tokens(0.0000526315789, decimals=None) == 0.0000526316
