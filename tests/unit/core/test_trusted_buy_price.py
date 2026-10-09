"""B4 (verifier round 3): a TRUSTED buy priced only by the exit-grade reading
accepted a DISPUTED quote (DexScreener and GeckoTerminal far apart). It now
refuses to verify the route on a dispute — the buy is held to the unchecked
ticket — while two agreeing sources (the owner's PNL buyback today: 1.3 %
apart) still verify it."""
from core.intel import price as P
from core.intel.model import SourceAnswer

ADDR = "0x" + "b" * 40


def _setup(monkeypatch, ds, gt):
    monkeypatch.setattr(P, "_SOURCES", [
        P.Source("dexscreener", lambda c, a: SourceAnswer(
            "dexscreener", usd=ds, confidence="low", liquidity_usd=80_000.0,
            pool_count=1), lambda c: True, primary=True),
        P.Source("geckoterminal", lambda c, a: SourceAnswer("geckoterminal", usd=gt),
                 lambda c: True),
    ])


def test_agreeing_sources_verify_a_trusted_buy(monkeypatch):
    _setup(monkeypatch, 3.612e-05, 3.565e-05)
    assert P.trusted_buy_price("robinhood", ADDR) == 3.612e-05


def test_a_disputed_quote_does_not(monkeypatch):
    _setup(monkeypatch, 3.612e-05, 1.0e-05)       # a pumped / lying primary
    assert P.trusted_buy_price("robinhood", ADDR) is None
    assert P.exit_price("robinhood", ADDR) == 3.612e-05   # an exit stays possible


def test_a_failed_second_source_leaves_the_buyback_as_it_was(monkeypatch):
    _setup(monkeypatch, 3.612e-05, None)
    assert P.trusted_buy_price("robinhood", ADDR) == 3.612e-05
