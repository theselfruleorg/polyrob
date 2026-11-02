"""`/send $22 <token> …` — a dollar amount the TOOL converts (prod 2026-10-28:
payout cards meant as $22 sent 22 tokens, ~$0.05; the model believed the tap
converts dollars). The ONE parser reads the `$` form; the converter uses the
send rail's spend-grade price and refuses without one — never a guess."""
import pytest

from tools.defi import send_order

ADDR = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


@pytest.mark.parametrize("word,usd", [
    ("$22", 22.0), ("$22.50", 22.5), ("22usd", 22.0), ("22USD", 22.0), ("$1,000", 1000.0),
])
def test_parse_reads_a_dollar_amount(word, usd):
    order, err = send_order.parse([word, TOKEN, "to", ADDR, "on", "base"])
    assert err is None
    assert order["usd"] == usd and order["amount"] is None and order["max_usd"] is None


def test_a_bare_number_stays_a_token_count():
    order, err = send_order.parse(["22", TOKEN, "to", ADDR, "on", "base"])
    assert err is None and order["amount"] == 22.0 and order["usd"] is None


@pytest.mark.parametrize("word", ["$0", "$", "$abc", "usd", "22$"])
def test_parse_refuses_a_bad_dollar_amount(word):
    order, err = send_order.parse([word, TOKEN, "to", ADDR, "on", "base"])
    assert order is None and err


def test_resolve_converts_at_the_spend_grade_price_and_implies_a_cap():
    order, _ = send_order.parse(["$22", TOKEN, "to", ADDR, "on", "base"])
    seen = []

    def price(chain, addr):
        seen.append((chain, addr))
        return 0.0000271

    out, px, why = send_order.resolve_usd(order, price)
    assert why is None and px == 0.0000271 and seen == [("base", TOKEN)]
    assert out["amount"] == pytest.approx(22 / 0.0000271, rel=1e-5)
    assert out["max_usd"] == round(22 * send_order.PRICE_ROOM + 0.01, 2)
    assert order["amount"] is None            # the parsed order is not mutated


def test_resolve_keeps_a_typed_max():
    order, _ = send_order.parse(["$22", TOKEN, "to", ADDR, "on", "base", "max", "22.5"])
    out, _, _ = send_order.resolve_usd(order, lambda c, a: 2.0)
    assert out["max_usd"] == 22.5 and out["amount"] == 11.0


@pytest.mark.parametrize("px", [None, 0.0, -1.0, float("nan")])
def test_resolve_refuses_without_a_spend_grade_price(px):
    order, _ = send_order.parse(["$22", TOKEN, "to", ADDR, "on", "base"])
    out, got, why = send_order.resolve_usd(order, lambda c, a: px)
    assert out is None and got is None
    assert "No spend-grade price" in why and "token amount" in why and "$22.00" in why


def test_native_prices_the_wrapped_native_like_the_guard():
    from core.wallet import chains
    order, _ = send_order.parse(["$30", "native", "to", ADDR, "on", "base"])
    seen = []
    out, _, _ = send_order.resolve_usd(order, lambda c, a: seen.append(a) or 3000.0)
    assert seen == [chains.get("base").wrapped_native]
    assert out["amount"] == 0.01 and out["token"] == "native"


def test_solana_pinned_usdc_is_one_dollar_by_definition():
    from core.wallet import chains
    usdc = chains.get("solana").usdc
    order, err = send_order.parse(["$5", usdc, "to", usdc, "on", "solana"])
    assert err is None
    out, px, _ = send_order.resolve_usd(order, lambda c, a: None)
    assert px == 1.0 and out["amount"] == 5.0


def test_a_token_order_passes_through_unpriced():
    order, _ = send_order.parse(["3", TOKEN, "to", ADDR, "on", "base"])
    out, px, why = send_order.resolve_usd(order, lambda c, a: 1 / 0)
    assert out is order and px is None and why is None


def test_the_shown_count_is_the_sent_count():
    assert send_order.round_tokens(811700.123456) == 811700.0
    assert send_order.round_tokens(0.00012345678) == 0.000123457
    assert send_order.fmt_tokens(811700.0) == "811,700"
    assert send_order.fmt_tokens(0.000123457) == "0.000123457"
    assert send_order.fmt_price(0.0000271) == "$0.00002710"
    assert send_order.fmt_price(2680.5) == "$2,680.50"


def test_usage_teaches_the_dollar_form():
    assert "$22" in send_order.USAGE and "TOKEN count" in send_order.USAGE
