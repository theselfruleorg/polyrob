"""084 WS6: one outvoted source must not knock a pinned stable off its peg.

Prod 2026-10-19: three USDG payouts passed, the fourth refused VALUE NOT
VERIFIED. ``_anchored`` read every answering source, including the one the
median had already outvoted.
"""
import pytest

from tools.intel import cache as intel_cache
from tools.intel import price as P
from tools.intel.model import HIGH, LOW, SourceAnswer

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"


@pytest.fixture
def registry():
    saved = P.registered_sources()
    P.unregister_all()
    intel_cache.clear_all()
    yield
    P.unregister_all()
    for s in saved:
        P.register_source(s.name, s.fn, covers=s.covers, primary=s.primary)
    intel_cache.clear_all()


def _ds(usd):
    return SourceAnswer(name="dexscreener", usd=usd, liquidity_usd=1_260_000.0,
                        priced_liquidity_usd=1_260_000.0, pool="0xpool", pool_count=1,
                        confidence=LOW)


def test_an_outvoted_source_does_not_break_the_peg(registry):
    P.register_source("dexscreener", lambda c, a: _ds(1.0001), primary=True)
    P.register_source("geckoterminal", lambda c, a: SourceAnswer("geckoterminal", usd=0.9995))
    P.register_source("thirdsource", lambda c, a: SourceAnswer("thirdsource", usd=0.90))
    q = P.quote("robinhood", USDG, use_cache=False)
    assert q.outlier == "thirdsource"
    assert q.confidence == HIGH and "peg" in q.reason
    assert P.spend_price("robinhood", USDG) == pytest.approx(1.0, abs=0.002)


def test_two_counted_sources_off_the_peg_still_grade_as_a_token(registry):
    P.register_source("dexscreener", lambda c, a: _ds(0.93), primary=True)
    P.register_source("geckoterminal", lambda c, a: SourceAnswer("geckoterminal", usd=0.931))
    q = P.quote("robinhood", USDG, use_cache=False)
    assert q.outlier is None
    assert q.confidence != HIGH and "OFF its" in (q.note or "")
    assert P.spend_price("robinhood", USDG) is None


def test_a_failed_owner_verb_logs_why():
    from surfaces.telegram.card_ops import _why_line
    text = ("❌ Nothing was sent. DRY RUN …\n⚠️ VALUE NOT VERIFIED — the only price is "
            "low-confidence\nTo continue: /send 18 x anyway")
    assert _why_line(text).startswith("❌ Nothing was sent")
    assert _why_line("all good") == "all good"
    assert _why_line("") == ""
