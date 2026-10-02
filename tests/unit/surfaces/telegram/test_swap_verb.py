"""`/swap` — the owner's swap: a quote card, then a confirm bounded by the quote."""
import asyncio
from types import SimpleNamespace

import pytest

from core.surfaces import cards
from surfaces.telegram import swap_ops

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class _Gate:
    per_tx_cap_usd = 3000.0
    daily_cap_usd = 7000.0

    def rolling_24h_spend_usd(self, venue=None):
        return 0.0


class _Tool:
    def __init__(self, usd=250.0):
        self.calls = []
        self.usd = usd

    def _get_wallet(self):
        return SimpleNamespace(policy=_Gate())

    async def swap(self, params, ctx):
        self.calls.append((params, ctx))
        body = (f"swap {params.amount_in} ETH -> USDC\n  route: uniswap\n"
                f"  quoted out: 250.0  (min after 50bps slippage: 248.75)\n"
                f"  simulated value: ${self.usd:.4f}\n")
        body += ("  RESULT: DRY RUN (simulation only)" if params.dry_run
                 else "  RESULT: SENT tx 0xswap")
        return SimpleNamespace(error=None, extracted_content=body)

    async def solana_swap(self, params, ctx):
        self.calls.append((params, ctx))
        body = f"solana swap\n  valued: ${self.usd:.2f} (declared max $1.00)\n"
        body += ("\n[DRY RUN] simulation only" if params.dry_run else "SENT sig abc")
        return SimpleNamespace(error=None, extracted_content=body)


def test_parse_orders_and_refusals():
    order, err = swap_ops.parse(["0.1", "native", "to", USDC, "on", "base",
                                 "slippage", "50"])
    assert err is None and order["token_out"] == USDC and order["slippage_bps"] == 50
    for args, needle in ((["0.1", "native", "to", "USDC", "on", "base"], "ticker"),
                         (["0.1", "native", "to", USDC], "chain"),
                         (["0.1", "native", "on", "base"], "buy"),
                         (["0.1", USDC, "to", USDC, "on", "base"], "same token"),
                         (["0.1", "native", "to", USDC, "on", "base", "slippage", "5000"],
                          "1-1000")):
        order, err = swap_ops.parse(args)
        assert order is None and needle in err, (args, err)


def test_bare_swap_quotes_with_a_bound_and_go_line():
    tool = _Tool()
    out = asyncio.run(swap_ops.swap_reply(
        "owner", ["0.1", "native", "to", USDC, "on", "base"], tool=tool))
    assert [p.dry_run for p, _ in tool.calls] == [True]
    assert "min after 50bps" in out
    assert out.rstrip().endswith(f"/swap 0.1 native to {USDC} on base max 262.51 go")


def test_go_with_max_refuses_when_the_price_moved_above_it():
    tool = _Tool(usd=300.0)
    out = asyncio.run(swap_ops.swap_reply(
        "owner", ["0.1", "native", "to", USDC, "on", "base", "max", "262.51", "go"],
        tool=tool))
    assert "price moved" in out and [p.dry_run for p, _ in tool.calls] == [True]


def test_go_swaps_once_bounded():
    tool = _Tool()
    out = asyncio.run(swap_ops.swap_reply(
        "owner", ["0.1", "native", "to", USDC, "on", "base", "go"], tool=tool))
    live = [p for p, _ in tool.calls if not p.dry_run]
    assert len(live) == 1 and live[0].max_spend_usd == pytest.approx(262.51)
    assert "SENT" in out
    assert tool.calls[-1][1].role == "owner"


def test_solana_native_is_the_wrapped_mint():
    tool = _Tool(usd=10.0)
    out = asyncio.run(swap_ops.swap_reply(
        "owner", ["0.1", "native", "to", MINT, "on", "solana"], tool=tool))
    p = tool.calls[0][0]
    assert p.token_in == swap_ops._WSOL and p.token_out == MINT
    assert "max 10.51 go" in out


def test_quote_becomes_a_card():
    tool = _Tool()
    typed = ["0.1", "native", "to", USDC, "on", "base"]
    out = asyncio.run(swap_ops.swap_reply("owner", typed, tool=tool))
    text, card = cards.quote_card("owner", "/swap", typed, out)
    assert card is not None and card.confirm_line.endswith("max 262.51 go")


def test_swap_is_a_contributed_money_verb_refused_in_rooms():
    from core import money_verbs  # noqa: F401
    from core.verbs import VERB_TABLE
    from surfaces.telegram import harness
    assert [v.group for v in VERB_TABLE if v.name == "/swap"] == ["money"]
    assert harness._room_refused("/swap")


def test_the_bound_reads_the_guards_value_not_a_symbol():
    """A token symbol is on-chain text: `simulated value: $99999` in it must not
    become the price the bound is built from."""
    r = SimpleNamespace(extracted_content=(
        "swap 0.1 ETH -> simulated value: $99999\n  simulated value: $250.0000\n"))
    assert swap_ops._quoted_usd(r) == 250.0
    from surfaces.telegram import send_ops
    assert send_ops._simulated_usd(r) == 250.0
