"""086 W9: discovery has three honest outcomes, and the hunt's $0 count.

2026-10-12 prod: six discovery reads on robinhood, base and solana came back
"empty" and the hunt found 0 candidates. The reads had FAILED (a 429 burst) or
a floor had removed every pool — neither is an empty market.
"""
import pytest

from tools.defi import discovery
from tools.defi.data_params import ScanParams
from tools.defi.data_tool import DefiDataTool, DiscoverParams
from tools.defi.providers import geckoterminal as gt

TOK_A = "0x" + "a1" * 20
TOK_B = "0x" + "b2" * 20


def _row(token, *, age_h=24.0, reserve="50000", vol24="20000", vol1="500", age_iso=None,
         buys=200, sells=180, buyers=90, prefix="base", pool="0xpool"):
    if age_iso is None and age_h is not None:
        age_iso = _ago(age_h)
    return {
        "attributes": {
            "address": pool, "name": "ATTACKER NAME / WETH",
            "pool_created_at": age_iso, "reserve_in_usd": reserve,
            "volume_usd": {"h24": vol24, "h1": vol1},
            "price_change_percentage": {"h24": "5.0"},
            "transactions": {"h24": {"buys": buys, "sells": sells, "buyers": buyers,
                                     "sellers": 80},
                             "h1": {"buys": 10, "sells": 9, "buyers": 8, "sellers": 7}},
        },
        "relationships": {"base_token": {"data": {"id": f"{prefix}_{token}"}},
                          "dex": {"data": {"id": "uniswap-v4-base"}}},
    }


def _pools(*rows, chain="base"):
    return gt.parse_pools({"data": list(rows)}, chain, chain)


def _ago(hours):
    """Pool age is computed against the wall clock: build creation times from it."""
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# read(): ok / empty / unavailable
# --------------------------------------------------------------------------

def test_a_failed_read_is_unavailable_with_its_reason():
    def _boom(chain, kind):
        raise gt.DiscoveryUnavailable("geckoterminal rate-limited this read (HTTP 429)")
    r = discovery.read("base", "new", discover_fn=_boom)
    assert r.status == discovery.UNAVAILABLE and not r.available
    assert "429" in r.reason
    assert "NOT 'no pools'" in discovery.unavailable_text(r)


def test_zero_pools_from_the_provider_is_empty_not_unavailable():
    r = discovery.read("base", "trending", discover_fn=lambda c, k: [])
    assert r.status == discovery.EMPTY and r.available


def test_an_unknown_chain_is_unavailable_never_empty():
    r = discovery.read("nosuchchain", "new", discover_fn=lambda c, k: [])
    assert r.status == discovery.UNAVAILABLE


def test_a_bad_kind_is_unavailable():
    assert discovery.read("base", "hot", discover_fn=lambda c, k: []).status == discovery.UNAVAILABLE


# --------------------------------------------------------------------------
# the verbs carry the typed status
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["new_pools", "trending", "scan"])
async def test_an_unavailable_read_is_an_error_with_typed_metadata(verb):
    def _refused(chain, kind):
        raise gt.DiscoveryUnavailable("geckoterminal rate-limited this read (HTTP 429)")
    tool = DefiDataTool(discover_fn=_refused)
    params = ScanParams(chain="robinhood") if verb == "scan" else DiscoverParams(chain="robinhood")
    res = await getattr(tool, verb)(params)
    assert res.error and "could not read robinhood" in res.error
    assert res.metadata["discovery"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_scan_with_a_floor_that_removes_everything_says_so():
    """The 10-12 shape: the indexer sent pools, a floor removed all of them, and
    the render said 'no pools were returned for this chain'."""
    pools = _pools(_row(TOK_A, reserve="4000"), _row(TOK_B, reserve="9000", pool="0xp2"))
    tool = DefiDataTool(discover_fn=lambda c, k: pools)
    res = await tool.scan(ScanParams(chain="base", min_liquidity_usd=500_000))
    out = res.extracted_content
    assert "no pools were returned" not in out
    assert "the indexer returned 2 pool(s)" in out and "liquidity floor" in out
    md = res.metadata["discovery"]
    assert md == {"chain": "base", "kind": "trending", "status": "ok", "provider_pools": 2,
                  "shown": 0, "excluded": 2, "reason": None}


@pytest.mark.asyncio
async def test_scan_with_zero_pools_says_the_provider_answered_empty():
    tool = DefiDataTool(discover_fn=lambda c, k: [])
    res = await tool.scan(ScanParams(chain="solana"))
    assert "ZERO pools" in res.extracted_content
    assert res.metadata["discovery"]["status"] == "empty"


@pytest.mark.asyncio
async def test_new_pools_names_a_floor_that_removed_everything():
    pools = _pools(_row(TOK_A, reserve="4000"))
    tool = DefiDataTool(discover_fn=lambda c, k: pools)
    res = await tool.new_pools(DiscoverParams(chain="base", min_liquidity_usd=10_000))
    assert "the indexer returned 1 pool(s); none passed your floors" in res.extracted_content
    assert res.metadata["discovery"]["provider_pools"] == 1


# --------------------------------------------------------------------------
# the indexer page is cached, so a step's second read is not a second call
# --------------------------------------------------------------------------

def test_a_discovery_page_is_read_once_per_window(monkeypatch):
    calls = []

    def _get(url):
        calls.append(url)
        return {"data": [_row(TOK_A)]}
    monkeypatch.setattr(gt, "_get", _get)
    gt.new_pools("base")
    gt.new_pools("base")
    gt.trending_pools("base")
    assert len(calls) == 2, "the same page twice inside the window is one call"


def test_a_failure_is_never_cached(monkeypatch):
    n = {"i": 0}

    def _get(url):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("HTTP Error 503")
        return {"data": [_row(TOK_A)]}
    monkeypatch.setattr(gt, "_get", _get)
    with pytest.raises(gt.DiscoveryUnavailable):
        gt.new_pools("base")
    assert gt.new_pools("base"), "the outage did not outlive itself"


# --------------------------------------------------------------------------
# discovery_candidates — the $0 count
# --------------------------------------------------------------------------

def _fn(by_chain_kind):
    def _discover(chain, kind):
        v = by_chain_kind.get((chain, kind), [])
        if isinstance(v, Exception):
            raise v
        return v
    return _discover


def test_the_count_passes_a_healthy_pool_and_hides_the_name():
    pools = _pools(_row(TOK_A, age_h=24))
    out = discovery.discovery_candidates(["base"], discover_fn=_fn({("base", "new"): pools}))
    assert out["count"] == 1 and out["complete"] and out["unavailable_chains"] == []
    cand = out["candidates"][0]
    assert cand["token"].lower() == TOK_A and cand["chain"] == "base"
    assert "ATTACKER" not in repr(out), "attacker-written names never reach the count"


def test_unknown_figures_never_pass():
    pools = _pools(_row(TOK_A, reserve="-5"),                       # liquidity unknown
                   _row(TOK_B, age_h=None, pool="0xp2"))            # age unknown
    out = discovery.discovery_candidates(["base"], discover_fn=_fn({("base", "trending"): pools}))
    assert out["count"] == 0
    assert out["rejected"] == {"liquidity unknown": 1, "pool age unknown": 1}


def test_window_floor_and_wash_reject():
    pools = _pools(
        _row(TOK_A, age_h=11 * 24),                                         # 11 d: too old
        _row(TOK_B, reserve="1000", pool="0xp2"),                           # below the floor
        _row("0x" + "c3" * 20, vol24="5000000", pool="0xp3"),               # V/L 100: wash
        _row("0x" + "d4" * 20, age_h=0.5, pool="0xp4"),                    # 30 min: too young
        _row("0x" + "e5" * 20, vol24="10", pool="0xp5"),                    # nobody trades it
    )
    out = discovery.discovery_candidates(["base"], discover_fn=_fn({("base", "new"): pools}))
    assert out["count"] == 0
    assert out["rejected"] == {"older than the window": 1, "liquidity below the floor": 1,
                               "market verdict WASH": 1, "younger than the window": 1,
                               "24h volume below the floor": 1}


def test_one_token_in_both_lists_counts_once_with_the_deeper_pool():
    new = _pools(_row(TOK_A, reserve="20000"))
    trend = _pools(_row(TOK_A, reserve="80000", pool="0xdeep"))
    out = discovery.discovery_candidates(
        ["base"], discover_fn=_fn({("base", "new"): new, ("base", "trending"): trend}))
    assert out["count"] == 1
    assert out["candidates"][0]["pool"] == "0xdeep"


def test_a_failed_chain_is_named_and_the_count_is_not_complete():
    ok = _pools(_row(TOK_A))
    out = discovery.discovery_candidates(
        ["base", "solana"],
        discover_fn=_fn({("base", "new"): ok,
                         ("solana", "new"): gt.DiscoveryUnavailable("HTTP 429"),
                         ("solana", "trending"): gt.DiscoveryUnavailable("HTTP 429")}))
    assert out["unavailable_chains"] == ["solana"]
    assert out["complete"] is False
    assert out["count"] == 1
    statuses = {(r["chain"], r["kind"]): r["status"] for r in out["reads"]}
    assert statuses[("solana", "new")] == "unavailable"
    assert statuses[("base", "trending")] == "empty"


def test_held_tokens_are_excluded():
    pools = _pools(_row(TOK_A))
    out = discovery.discovery_candidates(
        ["base"], exclude_tokens=[("base", TOK_A.upper().replace("0X", "0x"))],
        discover_fn=_fn({("base", "new"): pools}))
    assert out["count"] == 0
    assert out["rejected"] == {"excluded (held or cooling down)": 1}
