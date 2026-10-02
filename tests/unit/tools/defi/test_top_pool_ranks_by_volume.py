"""The pool a token TRADES in, not the pool that merely holds the most.

⚠️ The defect (2026-09-23, intel 03:25Z + the owner's own steer "drop gates just
measure properly"): ``_top_pool`` ranked purely on ``reserve_in_usd``, and
``ohlcv`` candles are POOL-scoped, so every price narrated through the resolver
described whichever pool held the biggest reserve — which on a token with many
pools is routinely not the one anybody trades.

Measured on PNL (Robinhood Chain) at 03:20Z:

===================  ===========  ==============  ==========
pool                 reserve      24 h volume     24 h change
===================  ===========  ==============  ==========
``0x43b7b259…``      $18,190      **$1,390**      +9.6%
``0x9c54d4e1…`` 1%   $4,147.81    **$61,792**     −84.17%
===================  ===========  ==============  ==========

**44x the volume on a quarter of the reserve.** A reviewer read the reserve-top
row, cross-checked it against a second index that ranks the same way, and filed
a RED FLAG against a rail that was correct — then had to retract it. The same
shape in the other direction is the 2026-09-22 "~96% collapse" report.

So the pick is now **the busiest pool**, because that is the one whose candles
describe the price a position can actually get. Two guardrails on that:

* Volume is only a ranking when it EXISTS. A token whose pools report no volume
  at all falls back to the deepest pool, which is the old behaviour and the
  right answer when nothing has traded.
* The choice never hides the alternative. When the deepest pool is a different
  pool, the pick carries it — address, reserve and volume — so a caller can
  always see the disagreement instead of inheriting a ranking it cannot audit.
  A volume-only pick is wash-tradeable; a pick that shows its work is not.
"""
import pytest

from tools.defi.providers.geckoterminal import PoolPick, _top_pool, top_pool_for_token


def _payload(*pools):
    """A ``/tokens/{addr}/pools`` body: each pool is (address, reserve, h24 volume)."""
    return {"data": [
        {"attributes": {"address": addr,
                        "reserve_in_usd": reserve,
                        "volume_usd": {"h24": vol}}}
        for addr, reserve, vol in pools
    ]}


def _pick(*pools, chain="robinhood"):
    return _top_pool(chain, "0x" + "b" * 40, fetch=lambda url: _payload(*pools))


#: The live shape this was built from.
PNL = (("0x43b7b259", "18190", "1390"), ("0x9c54d4e1", "4147.81", "61792"))


# --- the defect itself --------------------------------------------------------- #

def test_the_busiest_pool_wins_over_the_deepest():
    pick = _pick(*PNL)
    assert pick.address == "0x9c54d4e1"
    assert pick.liquidity_usd == pytest.approx(4147.81)
    assert pick.volume_h24_usd == pytest.approx(61792)


def test_the_deeper_pool_is_carried_so_the_disagreement_is_visible():
    """The whole reason a volume-only ranking would be no better: it must show
    its work, or the next reviewer inherits an unauditable pick."""
    pick = _pick(*PNL)
    assert pick.deepest_address == "0x43b7b259"
    assert pick.deepest_liquidity_usd == pytest.approx(18190)
    assert pick.deepest_volume_h24_usd == pytest.approx(1390)
    assert pick.pools_disagree is True


def test_listing_order_does_not_decide_it():
    """The indexer returns reserve-ordered rows; the pick must not inherit that."""
    forwards = _pick(*PNL)
    backwards = _pick(*reversed(PNL))
    assert forwards.address == backwards.address == "0x9c54d4e1"


# --- when the two agree, there is nothing to report ---------------------------- #

def test_one_pool_that_is_both_deepest_and_busiest_carries_no_alternative():
    pick = _pick(("0xonly", "5000", "900"), ("0xthin", "10", "1"))
    assert pick.address == "0xonly"
    assert pick.pools_disagree is False
    assert pick.deepest_address is None


def test_a_single_pool_is_the_answer():
    pick = _pick(("0xsolo", "1234", "99"))
    assert (pick.address, pick.pool_count, pick.pools_disagree) == ("0xsolo", 1, False)


# --- volume is a ranking only when it exists ----------------------------------- #

def test_no_pool_reports_volume_so_the_deepest_still_wins():
    """The pre-2026-09-23 behaviour, kept for the case it was right about: a token
    where nothing has traded has no 'busiest' pool to prefer."""
    pick = _pick(("0xdeep", "9000", None), ("0xshallow", "10", None))
    assert pick.address == "0xdeep"
    assert pick.volume_h24_usd is None
    assert pick.pools_disagree is False


def test_zero_volume_is_not_a_ranking_either():
    """The indexer sends 0 for a pool it has not valued — same artifact class the
    reserve parser already guards. It must not beat an unvalued pool by 0 > None
    or lose a genuinely-traded one."""
    pick = _pick(("0xdeep", "9000", "0"), ("0xtraded", "10", "5"))
    assert pick.address == "0xtraded"


def test_a_pool_with_volume_beats_a_deeper_pool_with_none():
    pick = _pick(("0xdeep", "9000", None), ("0xtraded", "10", "42"))
    assert pick.address == "0xtraded"
    assert pick.deepest_address == "0xdeep"


def test_an_unknown_volume_is_None_and_never_zero():
    """A zero reads as 'nothing traded here', which is a worse lie than silence."""
    pick = _pick(("0xsolo", "1234", None))
    assert pick.volume_h24_usd is None


# --- the shapes that were already right, still right --------------------------- #

def test_a_row_without_an_address_is_skipped_and_not_counted():
    pick = _pick(("", "9999", "9999"), ("0xreal", "10", "1"))
    assert (pick.address, pick.pool_count) == ("0xreal", 1)


def test_nothing_indexed_is_None_not_an_error():
    assert _top_pool("robinhood", "0x" + "c" * 40, fetch=lambda url: {"data": []}) is None


def test_a_provider_error_still_propagates():
    """A 429 is not 'no pool' — the action must say why, not report absence."""
    def _boom(url):
        raise RuntimeError("429 rate limited")
    with pytest.raises(RuntimeError, match="indexer error"):
        _top_pool("robinhood", "0x" + "d" * 40, fetch=_boom)


def test_the_address_only_helper_still_returns_a_string():
    addr = top_pool_for_token("robinhood", "0x" + "b" * 40,
                              fetch=lambda url: _payload(*PNL))
    assert addr == "0x9c54d4e1"


def test_the_old_three_field_construction_still_works():
    """`PoolPick("0xthin", 4251.64, 3)` is built positionally in existing tests and
    by any caller that predates the new fields."""
    pick = PoolPick("0xthin", 4251.64, 3)
    assert pick.volume_h24_usd is None
    assert pick.deepest_address is None
    assert pick.pools_disagree is False


# --- the answer a person reads names the basis and the road not taken ---------- #

def _rendered(depth):
    from tools.defi.data_tool import _render_candles
    return "\n".join(_render_candles([], None, chain="robinhood", token="0xPNL",
                                     pool=depth.address, timeframe="hour",
                                     aggregate=1, depth=depth))


def test_the_rendered_answer_says_it_read_the_busiest_pool_and_shows_the_volume():
    text = _rendered(_pick(*PNL))
    assert "busiest of 2 indexed pools" in text
    assert "$61,792" in text or "61,792" in text


def test_the_rendered_answer_names_the_deeper_pool_it_did_not_read():
    """The reader must be able to see that a choice was made — this exact line is
    what would have stopped the 03:25Z retraction from being needed."""
    text = _rendered(_pick(*PNL))
    assert "a DEEPER pool exists and was not read" in text
    assert "0x43b7b259" in text


def test_no_disagreement_line_when_there_is_no_disagreement():
    text = _rendered(_pick(("0xonly", "5000", "900")))
    assert "DEEPER pool exists" not in text
    assert "busiest of 1 indexed pool" in text


def test_a_fallback_to_depth_says_deepest_and_admits_the_unknown_volume():
    text = _rendered(_pick(("0xdeep", "9000", None), ("0xshallow", "10", None)))
    assert "deepest of 2 indexed pools" in text
    assert "24h volume unknown" in text
