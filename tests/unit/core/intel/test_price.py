"""071 W1: the one price read layer — aggregation, dispute, cache, seams.

Pure: every source here is a stub; nothing reaches the network.
"""
import pytest

from core.intel import cache as intel_cache
from core.intel import price as P
from core.intel.cache import TTLCache
from core.intel.model import DISPUTED, HIGH, LOW, SINGLE_POOL, UNKNOWN, PriceQuote, SourceAnswer

TOKEN = "0x" + "ab" * 20


@pytest.fixture(autouse=True)
def _fresh_registry():
    saved = P.registered_sources()
    P.unregister_all()
    intel_cache.clear_all()
    yield
    P.unregister_all()
    for s in saved:
        P.register_source(s.name, s.fn, covers=s.covers, primary=s.primary)
    intel_cache.clear_all()


def _ds(usd, *, conf=HIGH, liq=2_000_000.0, pools=3, pool="0xpool"):
    return SourceAnswer(name="dexscreener", usd=usd, liquidity_usd=liq,
                        priced_liquidity_usd=liq, pool=pool, pool_count=pools,
                        confidence=conf)


# -- aggregate ----------------------------------------------------------------

def test_one_source_keeps_its_own_grade_and_price():
    q = P.aggregate([_ds(1.0)])
    assert q.usd == 1.0 and q.confidence == HIGH and q.primary_usd == 1.0
    assert q.spread_pct is None and q.sources == (("dexscreener", 1.0),)


def test_the_quote_is_the_median_of_the_sources():
    q = P.aggregate([_ds(1.00), SourceAnswer("geckoterminal", usd=1.01),
                     SourceAnswer("jupiter", usd=1.005)])
    assert q.usd == pytest.approx(1.005)
    assert q.confidence == HIGH
    assert q.spread_pct == pytest.approx(0.01 / 1.005 * 100)


def test_a_liquid_pool_notes_a_small_gap_and_disputes_only_above_ten_percent():
    """Eased 2026-10-02: 3 % on a liquid pool is a NOTE (grade kept); >10 % disputes."""
    noted = P.aggregate([_ds(1.00, liq=5_000_000), SourceAnswer("geckoterminal", usd=1.03)])
    assert noted.confidence == HIGH and not noted.disputed
    assert noted.note and "within" in noted.note
    q = P.aggregate([_ds(1.00, liq=5_000_000), SourceAnswer("geckoterminal", usd=1.12)])
    assert q.confidence == DISPUTED and q.disputed
    assert "disagree" in q.reason


def test_a_thin_pool_tolerates_up_to_twenty_five_percent():
    agree = P.aggregate([_ds(1.00, conf=LOW, liq=60_000), SourceAnswer("gt", usd=1.04)])
    assert agree.confidence == LOW and agree.note is None
    noted = P.aggregate([_ds(1.00, conf=LOW, liq=60_000), SourceAnswer("gt", usd=1.20)])
    assert noted.confidence == LOW and noted.note
    split = P.aggregate([_ds(1.00, conf=LOW, liq=60_000), SourceAnswer("gt", usd=1.30)])
    assert split.confidence == DISPUTED


def test_one_outvoted_secondary_does_not_dispute():
    q = P.aggregate([_ds(1.00, liq=5_000_000), SourceAnswer("geckoterminal", usd=1.002),
                     SourceAnswer("jupiter", usd=1.40)])
    assert q.confidence == HIGH and "jupiter is an outlier" in q.note
    assert q.usd == pytest.approx(1.001)


def test_the_primary_is_never_the_excused_outlier():
    q = P.aggregate([_ds(1.40, liq=5_000_000), SourceAnswer("geckoterminal", usd=1.00),
                     SourceAnswer("jupiter", usd=1.002)])
    assert q.confidence == DISPUTED


def test_a_secondary_source_can_never_grant_high():
    q = P.aggregate([_ds(1.0, conf=LOW, pools=1, liq=10_000),
                     SourceAnswer("geckoterminal", usd=1.0)])
    assert q.confidence == LOW


def test_a_price_only_a_secondary_supplied_is_low():
    q = P.aggregate([SourceAnswer("dexscreener", usd=None, confidence=UNKNOWN, pool_count=0),
                     SourceAnswer("geckoterminal", usd=2.0)])
    assert q.usd == 2.0 and q.confidence == LOW and q.primary_usd is None


def test_a_failed_source_is_named_never_zero():
    q = P.aggregate([_ds(1.0), SourceAnswer("geckoterminal", error="RuntimeError")])
    assert q.usd == 1.0 and q.confidence == HIGH
    assert q.failed == (("geckoterminal", "RuntimeError"),)
    assert all(v > 0 for _, v in q.sources)


def test_every_source_failing_is_unknown_with_the_reason():
    q = P.aggregate([SourceAnswer("dexscreener", error="dexscreener did not answer"),
                     SourceAnswer("geckoterminal", error="HttpStatusError")])
    assert q.usd is None and q.confidence == UNKNOWN
    assert "did not answer" in q.reason and "geckoterminal" in q.reason


def test_a_zero_or_nan_price_is_unpriced_not_a_value():
    q = P.aggregate([_ds(0.0), SourceAnswer("gt", usd=float("nan"))])
    assert q.usd is None and q.confidence == UNKNOWN


def test_single_pool_is_a_display_label_only():
    q = P.aggregate([_ds(1.0, conf=LOW, pools=1, liq=10_000)])
    assert q.confidence == LOW
    assert q.display_confidence == SINGLE_POOL


def test_the_quote_reads_like_a_price_info():
    q = P.aggregate([_ds(1.5, pool="0xabc")])
    assert q.price_usd == 1.5 and q.priced_pool_address == "0xabc"
    md = q.to_metadata()
    assert md["usd"] == 1.5 and md["sources"] == [{"name": "dexscreener", "usd": 1.5}]


# -- quote(): registry, cache, prefetched answers ----------------------------

def test_no_source_registered_is_unknown_and_says_so():
    q = P.quote("base", TOKEN)
    assert q.usd is None and q.confidence == UNKNOWN
    assert "no price source" in q.reason


def test_quote_asks_each_covering_source_and_caches_a_full_answer():
    calls = []

    def ds(chain, addr):
        calls.append("ds")
        return _ds(1.0)

    def gt(chain, addr):
        calls.append("gt")
        return SourceAnswer("geckoterminal", usd=1.001)

    P.register_source("dexscreener", ds, primary=True)
    P.register_source("geckoterminal", gt)
    P.register_source("jupiter", lambda c, a: SourceAnswer("jupiter", usd=99.0),
                      covers=lambda chain: chain == "solana")
    q1 = P.quote("base", TOKEN)
    q2 = P.quote("base", TOKEN.upper().replace("0X", "0x"))
    assert calls == ["ds", "gt"]            # second read served by the cache
    assert q1.usd == q2.usd and q2.confidence == HIGH
    assert [n for n, _ in q1.sources] == ["dexscreener", "geckoterminal"]


def test_a_raising_source_is_a_named_failure_and_is_not_cached():
    calls = []

    def boom(chain, addr):
        calls.append(1)
        raise TimeoutError("slow")

    P.register_source("dexscreener", lambda c, a: _ds(1.0), primary=True)
    P.register_source("geckoterminal", boom)
    q = P.quote("base", TOKEN)
    assert q.failed == (("geckoterminal", "TimeoutError"),)
    P.quote("base", TOKEN)
    assert len(calls) == 2


def test_prefetched_answers_replace_the_call():
    P.register_source("dexscreener", lambda c, a: _ds(1.0), primary=True)
    P.register_source("geckoterminal", lambda c, a: pytest.fail("must not be asked"))
    q = P.quote("base", TOKEN, answers={"geckoterminal": SourceAnswer("geckoterminal", usd=1.0)})
    assert q.confidence == HIGH


def test_spend_price_is_the_primary_number_and_only_when_high():
    P.register_source("dexscreener", lambda c, a: _ds(1.00), primary=True)
    P.register_source("geckoterminal", lambda c, a: SourceAnswer("geckoterminal", usd=1.01))
    assert P.spend_price("base", TOKEN) == 1.00      # never the median


def test_a_dispute_removes_every_spend_price():
    P.register_source("dexscreener", lambda c, a: _ds(1.00), primary=True)
    P.register_source("geckoterminal", lambda c, a: SourceAnswer("geckoterminal", usd=1.50))
    assert P.spend_price("base", TOKEN) is None
    # 071 review: an EXIT is never blocked by a dispute (stop-loss liveness).
    assert P.exit_price("base", TOKEN) == 1.00
    assert P.indexer_price("base", TOKEN) is None
    assert P.quote("base", TOKEN).usd is not None    # still SHOWN


def test_exit_price_keeps_the_028_rules():
    P.register_source("dexscreener", lambda c, a: _ds(0.42, conf=LOW, liq=25_000, pools=1),
                      primary=True)
    assert P.exit_price("base", TOKEN) == 0.42
    intel_cache.clear_all()
    P.register_source("dexscreener", lambda c, a: _ds(0.42, conf=LOW, liq=0.0, pools=1),
                      primary=True)
    assert P.exit_price("base", TOKEN) is None
    intel_cache.clear_all()
    P.register_source("dexscreener", lambda c, a: _ds(0.42, conf=UNKNOWN), primary=True)
    assert P.exit_price("base", TOKEN) is None


def test_solana_keys_stay_case_sensitive():
    assert P.cache_key("solana", "AbC") != P.cache_key("solana", "abc")
    assert P.cache_key("base", "0xAbC") == P.cache_key("base", "0xabc")


# -- cache ---------------------------------------------------------------------

def test_ttl_cache_expires_on_the_injected_clock():
    now = [100.0]
    c = TTLCache(20.0, clock=lambda: now[0])
    c.put("k", "v")
    now[0] = 115.0
    assert c.get_with_age("k") == ("v", 15.0)
    now[0] = 121.0
    assert c.get("k") is None


def test_immutable_cache_never_expires():
    now = [0.0]
    c = TTLCache(None, clock=lambda: now[0])
    c.put("k", 1)
    now[0] = 1e9
    assert c.get("k") == 1


def test_the_cache_is_bounded():
    now = [0.0]
    c = TTLCache(None, clock=lambda: now[0], max_items=2)
    for i in range(3):
        now[0] = float(i)
        c.put(i, i)
    assert len(c) == 2 and c.get(0) is None


def test_quote_is_a_price_quote():
    assert isinstance(P.aggregate([]), PriceQuote)
