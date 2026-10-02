"""defi_data.pool_metrics — the 090 §3 monitoring read (core handoff W9)."""
import pytest

from core.wallet import dex_registry
from tools.defi import lp_reads, lp_v4 as V, pool_metrics as PM

PNL = "0xbba60ab93fc409b1a34371cbf6c3173795ed2c7e"
POOL_ID = "0x43b7b259007600ad19df15a23b620fd77c8c121c8ecc8788d5116a833d031e94"
SQRT = 627219137286278685928990650320595        # measured 2026-09-29, 4663 fork
LIQ = 29277002188455997052109
KEY = lp_reads.pons_pool_key({"token": PNL, "pairToken": "0x" + "0" * 40,
                              "poolFee": 0, "tickSpacing": 200})


@pytest.fixture
def reads(monkeypatch):
    monkeypatch.setattr(V, "pons_key_for", lambda rpc, token: KEY)
    monkeypatch.setattr(dex_registry, "verify_pins", lambda *a: None)
    monkeypatch.setattr(dex_registry, "verify_hook", lambda *a: None)
    monkeypatch.setattr(V, "pool_state", lambda *a: V.V4PoolState(
        POOL_ID, SQRT, 179543, 0, 0, LIQ, "state_view"))
    monkeypatch.setattr(lp_reads, "decimals", lambda *a: 18)
    recorded = []
    return recorded


def _run(recorded, rows=(), **kw):
    return PM.pool_metrics(None, "robinhood", PNL, now=10 ** 9,
                           query_fn=lambda **q: list(rows),
                           record_fn=lambda kind, **a: recorded.append((kind, a)), **kw)


def test_reserves_price_and_impact_match_090(reads):
    out = _run(reads)
    assert out["pool_id"] == POOL_ID and not out["alerts"]
    assert 3.69 < out["reserve0"] < 3.71                       # ETH side, measured ≈3.698
    assert 6.26e7 < out["token_per_native"] < 6.28e7
    approx = 2 * 0.021 * 0.99 / out["reserve0"] * 100          # 090 §3 formula
    assert abs(out["mint_impact_pct"] - approx) < 0.01
    assert reads and reads[0][0] == PM.EVENT_KIND              # each reading is journaled
    assert reads[0][1]["attrs"]["pool_id"] == POOL_ID


def test_depth_is_consistent_with_constant_liquidity():
    c0_in, c1_out, c1_in, c0_out = PM.depth_for_move(SQRT, LIQ, 1.0)
    x = LIQ / (SQRT / 2 ** 96)
    # lifting the token price 1% takes ≈ x·(sqrt(1.01) − 1) of currency0
    assert abs(c0_in / x - (1.01 ** 0.5 - 1)) < 1e-9
    assert c1_out > 0 and c1_in > 0 and c0_out > 0
    assert PM.depth_for_move(SQRT, LIQ, 100.0)[2] == float("inf")


def test_history_gives_24h_change_and_the_median_reference(reads):
    price_now = _run(reads)["price"]
    rows = [{"ts": 10 ** 9 - 86400 + 600, "attrs": {"pool_id": POOL_ID, "chain": "robinhood",
                                                   "price": price_now / 1.1}}]
    rows += [{"ts": 10 ** 9 - 60 * i, "attrs": {"pool_id": POOL_ID, "chain": "robinhood",
                                               "price": price_now}} for i in range(1, 4)]
    rows.sort(key=lambda r: -r["ts"])
    out = _run(reads, rows)
    assert abs(out["change_24h_pct"] - 10.0) < 1e-6
    assert out["reference_price"] == price_now
    other = [dict(r, attrs=dict(r["attrs"], pool_id="0x" + "0" * 64)) for r in rows]
    out = _run(reads, other)
    assert out["change_24h_pct"] is None
    assert any("not derivable" in line for line in out["lines"])


def test_an_unreadable_history_is_not_derivable_not_zero(reads):
    def boom(**q):
        raise RuntimeError("db locked")
    out = PM.pool_metrics(None, "robinhood", PNL, now=10 ** 9, query_fn=boom,
                          record_fn=lambda *a, **k: None)
    assert out["change_24h_pct"] is None
    assert any("unreadable" in line for line in out["lines"])


def test_a_changed_code_hash_is_an_alert_first(reads, monkeypatch):
    def changed(*a):
        raise dex_registry.DexPinError("the code at 0xhook hashes to 0xnew")
    monkeypatch.setattr(dex_registry, "verify_hook", changed)
    out = _run(reads)
    assert out["lines"][0].startswith("ALERT: code hash check failed")
    assert out["pins_ok"] is False


def test_an_unexpected_pool_id_is_an_alert(reads):
    out = _run(reads, expect_pool_id="0x" + "1" * 64)
    assert out["lines"][0].startswith("ALERT: the Pons PoolKey")
