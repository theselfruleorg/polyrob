"""Two contracts in one wallet claiming one ticker must be said out loud.

⚠️ The defect, traced to its exact step in production on 2026-09-23. The SAFETY
monitor picked the token it was monitoring out of the PORTFOLIO, by symbol. Its
own words, step 3 of 20 (session ``498136c7``):

    "PNL token address is 0x357A04366240aa3c9d916Aa0F15c3033686C9007
     (615.165420 PNL held, value excluded) — that is the PNL contract."

It was not. The treasury holds **both** `0xbBa60AB9…` (99,090,018 PNL, the
buyback position) **and** `0x357A0436…` (615 tokens of a different contract using
the same ticker, received as dust). Every measurement after that premise —
`token_info`, `token_holders`, both swap-quote legs — was a correct reading of
the wrong token, and the owner was told PNL liquidity had collapsed 78% when the
real holding was down 3.8%.

The rail was not careless: it reasoned soundly all the way down, and even
reconciled a bogus concentration spike correctly. The premise was wrong one step
earlier, on the screen this test is about.

Nothing downstream could have caught it. `token_info` already prints the contract
it was asked about, and a price already names the pool it came from — both answer
honestly about an address **chosen before either was called**. So the warning has
to live where the choice is made.

The check is deliberately dumb and local: it compares the symbols of the rows
this wallet actually holds. It does not ask any provider which ticker is
"legitimate" — that question has no honest answer, and a seeded look-alike can
outrank a real token on every ranking a provider offers.
"""
import pytest

from tools.defi.data_tool import _ticker_collision_lines

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40


# --- the discriminator ---------------------------------------------------------- #

def test_one_contract_per_symbol_says_nothing():
    assert _ticker_collision_lines([("PNL", A), ("USDC", B)]) == []


def test_two_contracts_sharing_a_symbol_are_named():
    out = "\n".join(_ticker_collision_lines([("PNL", A), ("PNL", B)]))
    assert "PNL" in out
    assert A in out and B in out


def test_the_warning_says_what_to_do_about_it():
    """A flag that does not say what it means gets read as noise."""
    out = "\n".join(_ticker_collision_lines([("PNL", A), ("PNL", B)])).lower()
    assert "symbol" in out
    assert "address" in out


def test_three_contracts_are_all_listed():
    out = "\n".join(_ticker_collision_lines([("PNL", A), ("PNL", B), ("PNL", C)]))
    assert A in out and B in out and C in out


def test_symbols_differing_only_in_case_still_collide():
    """`PnL / WETH 1%` and `PNL / WETH 1%` are both live on this chain."""
    out = "\n".join(_ticker_collision_lines([("PNL", A), ("pnl", B)]))
    assert A in out and B in out


def test_surrounding_whitespace_does_not_hide_a_collision():
    out = "\n".join(_ticker_collision_lines([(" PNL ", A), ("PNL", B)]))
    assert A in out and B in out


def test_the_same_contract_listed_twice_is_not_a_collision():
    """One row per contract is the norm; a duplicate is not two tokens."""
    assert _ticker_collision_lines([("PNL", A), ("PNL", A)]) == []


def test_an_unknown_symbol_is_not_collided_with_another_unknown():
    """'?' is what the render prints when identity is unreadable. Two unknowns
    are not evidence of a shared ticker — claiming they are would manufacture a
    warning out of missing data."""
    assert _ticker_collision_lines([("?", A), ("?", B)]) == []
    assert _ticker_collision_lines([(None, A), (None, B)]) == []
    assert _ticker_collision_lines([("", A), ("", B)]) == []


def test_a_real_symbol_still_collides_when_an_unknown_is_present():
    out = "\n".join(_ticker_collision_lines([("PNL", A), ("PNL", B), ("?", C)]))
    assert A in out and B in out
    assert C not in out


def test_an_empty_wallet_says_nothing():
    assert _ticker_collision_lines([]) == []


# --- the production case, in miniature ------------------------------------------ #

PNL_REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
PNL_DUST = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"


def test_the_two_PNL_contracts_that_caused_this():
    out = "\n".join(_ticker_collision_lines(
        [("JUGGERNAUT", A), ("PNL", PNL_REAL), ("PNL", PNL_DUST), ("SHROOM", B)]))
    assert PNL_REAL in out and PNL_DUST in out
    assert "JUGGERNAUT" not in out and "SHROOM" not in out


# --- the NAME is what actually tells them apart ---------------------------------- #

def test_the_name_is_shown_beside_each_address():
    """Read live from chain 2026-09-23: both contracts report symbol 'PNL', and
    their NAMES are 'Rob Track Record' and 'Pissin N Lying'. The symbol cannot
    separate them and the address is 42 characters of hex; the name is the field
    a human actually reads. Showing it is not ranking them — it is handing over
    the other identifier the contract already publishes."""
    out = "\n".join(_ticker_collision_lines(
        [("PNL", PNL_REAL, "Rob Track Record"), ("PNL", PNL_DUST, "Pissin N Lying")]))
    assert "Rob Track Record" in out and "Pissin N Lying" in out
    assert PNL_REAL in out and PNL_DUST in out


def test_a_missing_name_is_said_to_be_missing_not_blank():
    out = "\n".join(_ticker_collision_lines([("PNL", A, None), ("PNL", B, "Real Thing")]))
    assert "Real Thing" in out
    assert "name unknown" in out.lower()


def test_rows_without_a_name_field_still_work():
    """Every caller predates the third element; two-tuples must keep working."""
    out = "\n".join(_ticker_collision_lines([("PNL", A), ("PNL", B)]))
    assert A in out and B in out


def test_the_warning_does_not_pick_a_winner():
    """Neither contract is LABELLED. Liquidity, balance and volume are all
    purchasable, so there is no honest basis for ranking them here — the caller
    must resolve the address from its own records.

    Scoped to the lines that name an address, deliberately: the prose above them
    is allowed to use words like "real" to explain why ranking cannot settle
    this. What must never happen is a verdict attached to a CONTRACT.
    """
    lines = _ticker_collision_lines([("PNL", PNL_REAL), ("PNL", PNL_DUST)])
    addressed = [ln.lower() for ln in lines if PNL_REAL in ln or PNL_DUST in ln]
    assert len(addressed) == 2                      # both listed, nothing else
    for ln in addressed:
        for claim in ("real", "genuine", "legitimate", "fake", "scam",
                      "correct", "wrong", "main", "official"):
            assert claim not in ln, f"{claim!r} labels a contract: {ln!r}"
