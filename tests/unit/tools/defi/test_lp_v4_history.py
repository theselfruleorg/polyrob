"""118 W3: a v4 position's result against holding, the pool's depth and its
liquidity holders — all from the PoolManager's logs, with a fake node."""
from types import SimpleNamespace

import pytest

from core.wallet import abi, dex_registry, univ3_math as M
from tools.defi import lp_v4 as V, lp_v4_history as H
from tools.defi.lp_reads import LpReadError

CHAIN = "robinhood"
PID = "0x" + "ab" * 32
ROW = dex_registry.row_for(CHAIN, "v4")
POSM = "0x" + "0" * 24 + ROW.position_manager.lower()[2:]
OTHER = "0x" + "0" * 24 + "9" * 40
LO, HI = -200, 200
S0 = M.sqrt_price_at_tick(0)          # price at the deposit
S1 = M.sqrt_price_at_tick(100)        # price after one swap
OWNER = "0x" + "7" * 40


def _w(n: int) -> str:
    return format(n % (1 << 256), "064x")


def _log(topic, block, index, words, sender=POSM):
    return {"topics": [topic, PID, sender], "blockNumber": hex(block), "logIndex": hex(index),
            "data": "0x" + "".join(_w(n) for n in words)}


def _node(logs, head=1000):
    def rpc(method, params):
        if method == "eth_blockNumber":
            return hex(head)
        if method == "eth_getLogs":
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            return [l for l in logs if l["topics"][0] == q["topics"][0]
                    and lo <= int(l["blockNumber"], 16) <= hi]
        if method == "eth_call":      # ownerOf
            return "0x" + "0" * 24 + OWNER[2:]
        raise AssertionError(method)
    return rpc


def _init(block=10, sqrt=S0):
    return _log(H.topic_initialize(), block, 0, [10000, 200, 0, sqrt, 0])


def _change(block, index, delta, salt=7, lo=LO, hi=HI, sender=POSM):
    return _log(V.TOPIC_MODIFY_LIQUIDITY, block, index, [lo, hi, delta, salt], sender)


def _swap(block, index, sqrt):
    return _log(V.TOPIC_SWAP, block, index, [-5, 5, sqrt, 0, 0, 10000])


def _pos(liquidity, token_id=7):
    return SimpleNamespace(token_id=token_id, pool_id=PID, tick_lower=LO, tick_upper=HI,
                           liquidity=liquidity)


def test_the_price_of_a_change_is_the_last_swap_before_it_or_the_initialize_price():
    logs = [_init(), _change(10, 1, 10 ** 18), _swap(20, 0, S1), _change(20, 1, 10 ** 18),
            _swap(30, 0, S0)]
    rpc = _node(logs)
    hist = H.pool_history(rpc, CHAIN, PID)
    assert [c.block for c in hist.changes] == [10, 20] and hist.init_sqrt == S0
    assert H.price_at(rpc, CHAIN, PID, hist, hist.changes[0]) == S0
    # The swap in the same block with a LOWER log index counts; the later one does not.
    assert H.price_at(rpc, CHAIN, PID, hist, hist.changes[1]) == S1


def test_flows_value_each_change_at_its_own_price_and_must_sum_to_the_position():
    logs = [_init(), _change(10, 1, 10 ** 18), _swap(20, 0, S1), _change(20, 1, -4 * 10 ** 17),
            _change(21, 0, 5 * 10 ** 17, salt=8)]            # another position: not counted
    rpc = _node(logs)
    hist = H.pool_history(rpc, CHAIN, PID)
    flows = H.position_flows(rpc, CHAIN, _pos(6 * 10 ** 17), hist)
    sa, sb = M.sqrt_price_at_tick(LO), M.sqrt_price_at_tick(HI)
    assert (flows["in0"], flows["in1"]) == M.amounts_for_liquidity(S0, sa, sb, 10 ** 18)
    assert (flows["out0"], flows["out1"]) == M.amounts_for_liquidity(S1, sa, sb, 4 * 10 ** 17)
    assert flows["changes"] == 2
    with pytest.raises(LpReadError, match="logs sum to liquidity"):
        H.position_flows(rpc, CHAIN, _pos(10 ** 18), hist)
    with pytest.raises(LpReadError, match="no liquidity change"):
        H.position_flows(rpc, CHAIN, _pos(1, token_id=99), hist)


def test_result_is_zero_at_the_deposit_price_and_negative_after_a_move():
    sa, sb = M.sqrt_price_at_tick(LO), M.sqrt_price_at_tick(HI)
    rpc = _node([_init(), _change(10, 1, 10 ** 18)])
    pos = _pos(10 ** 18)
    same = H.result_lines(rpc, CHAIN, pos, SimpleNamespace(sqrt_price_x96=S0),
                          M.amounts_for_liquidity(S0, sa, sb, 10 ** 18), (0, 0), 18, 18)
    assert "= +0 (+0.0%)" in same[-1] and "fees owed are counted" in same[-1]
    moved = H.result_lines(rpc, CHAIN, pos, SimpleNamespace(sqrt_price_x96=S1),
                           M.amounts_for_liquidity(S1, sa, sb, 10 ** 18), None, 18, 18)
    assert "= -" in moved[-1] and "NOT counted" in moved[-1]
    assert "1 change(s)" in moved[0] and "withdrawn" not in moved[0]


def test_an_unreadable_history_is_not_read_never_a_number():
    def rpc(method, params):
        raise RuntimeError("node down")
    st = SimpleNamespace(sqrt_price_x96=S0, tick=0, liquidity=10 ** 18)
    assert "NOT READ" in H.result_lines(rpc, CHAIN, _pos(1), st, (0, 0), None, 18, 18)[0]
    assert "NOT READ" in H.holder_lines(rpc, CHAIN, PID, st)[0]
    # A pool older than the walk reaches is unknown, not "no deposits".
    with pytest.raises(LpReadError, match="older than the log walk"):
        H.pool_history(_node([_change(10, 1, 5)]), CHAIN, PID)


def test_depth_is_the_currency0_that_moves_the_price():
    st = SimpleNamespace(sqrt_price_x96=S0, liquidity=10 ** 20)   # x = 100 currency0
    line = H.depth_lines(st, 18)[0]
    # 2% down in price: x * (1/sqrt(0.98) - 1) = 1.0153...; 10%: 5.409...
    assert "1.015" in line and "5.409" in line and "held constant" in line
    assert "no liquidity" in H.depth_lines(SimpleNamespace(sqrt_price_x96=S0, liquidity=0), 18)[0]


def test_holders_are_net_positions_and_must_sum_to_the_pool():
    logs = [_init(), _change(10, 1, 8 * 10 ** 18), _change(11, 0, 2 * 10 ** 18, salt=8),
            _change(12, 0, 10 ** 18, salt=9, lo=400, hi=800),          # out of range
            _change(13, 0, 5 * 10 ** 17, salt=1, sender=OTHER),
            _change(14, 0, -5 * 10 ** 17, salt=1, sender=OTHER)]       # emptied
    rpc = _node(logs)
    st = SimpleNamespace(tick=0, liquidity=10 ** 19)
    h = H.holders(rpc, CHAIN, PID, st)
    assert (h["positions"], h["in_range"]) == (3, 2)
    line = H.holder_lines(rpc, CHAIN, PID, st)[0]
    assert "80.0% position #7 [-200,200]" in line and OWNER in line and "20.0% position #8" in line
    short = SimpleNamespace(tick=0, liquidity=9 * 10 ** 18)
    assert "NOT READ" in H.holder_lines(rpc, CHAIN, PID, short)[0]


def test_position_facts_are_typed_and_a_missing_fee_read_drops_the_value(monkeypatch):
    """118 W4: the fields a $0 watch compares."""
    sa, sb = M.sqrt_price_at_tick(LO), M.sqrt_price_at_tick(HI)
    key = {"currency0": "0x" + "0" * 40, "currency1": "0x" + "5" * 40}
    pos = SimpleNamespace(token_id=7, owner=OWNER, key=key, pool_id=PID, tick_lower=LO,
                          tick_upper=HI, liquidity=10 ** 18)
    st = {"tick": 0, "sqrt": S0}
    monkeypatch.setattr(V, "position", lambda *a: pos)
    monkeypatch.setattr(V, "pool_state", lambda *a: SimpleNamespace(
        sqrt_price_x96=st["sqrt"], tick=st["tick"], liquidity=4 * 10 ** 18))
    monkeypatch.setattr(V, "position_fees", lambda *a: (10 ** 15, 0))
    f = V.position_facts(None, CHAIN, 7, lambda t: 18)
    a0, a1 = M.amounts_for_liquidity(S0, sa, sb, 10 ** 18)
    assert f["in_range"] == "yes" and f["pool_share_pct"] == 25.0
    assert f["currency0"] == a0 / 1e18 and f["price"] == pytest.approx(1.0)
    assert f["value0"] == pytest.approx((a0 + a1 + 10 ** 15) / 1e18)
    st.update(tick=300, sqrt=M.sqrt_price_at_tick(300))
    out = V.position_facts(None, CHAIN, 7, lambda t: 18)
    assert out["in_range"] == "no" and out["currency0"] == 0 and out["pool_share_pct"] == 0.0

    def no_fees(*a):
        raise LpReadError("x")
    monkeypatch.setattr(V, "position_fees", no_fees)
    assert "value0" not in V.position_facts(None, CHAIN, 7, lambda t: 18)


def test_a_position_named_by_id_is_a_read_job_verb_with_an_out_of_range_alert():
    from cron import read_job as RJ
    spec = RJ.validate_read_verb({"verb": "defi_data.lp_positions",
                                   "params": {"chain": CHAIN, "protocol": "v4", "token_id": 7},
                                   "alert": [{"field": "in_range", "equals": "no"},
                                             {"field": "currency0", "below": 0.2}]})
    assert RJ.alert_lines(spec["alert"], {"in_range": "yes", "currency0": 0.25}) == []
    assert len(RJ.alert_lines(spec["alert"], {"in_range": "no", "currency0": 0.1})) == 2


# --- 118 W8: the rows a surface lists -------------------------------------

def test_v4_held_rows_names_what_was_not_found(monkeypatch):
    from types import SimpleNamespace
    from tools.defi import lp_holdings as H
    holder = "0x" + "aa" * 20
    monkeypatch.setattr(H.dex_registry, "row_for",
                        lambda chain, protocol: SimpleNamespace(position_manager="0x" + "11" * 20))
    monkeypatch.setattr(H.R, "view", lambda rpc, to, sig, args: 3)
    monkeypatch.setattr(H, "_v4_ids", lambda *a: [5, 6])

    def position(rpc, chain, token_id):
        if token_id == 6:
            raise H.R.LpReadError("node down")
        return SimpleNamespace(owner=holder.upper().replace("0X", "0x"), tick_lower=-200,
                               tick_upper=200, liquidity=9,
                               key={"currency0": "0x0", "currency1": "0x1", "fee": 10000})
    monkeypatch.setattr(H.V, "position", position)
    rows = H.v4_held_rows(None, None, "c", holder)
    assert rows[0]["token_id"] == 5 and rows[0]["protocol"] == "v4" and "note" not in rows[0]
    assert "NOT READ" in rows[1]["note"]
    assert rows[2]["token_id"] is None and "1 more" in rows[2]["note"]


def test_v4_held_rows_is_empty_only_on_a_read_zero(monkeypatch):
    from types import SimpleNamespace
    from tools.defi import lp_holdings as H
    monkeypatch.setattr(H.dex_registry, "row_for",
                        lambda chain, protocol: SimpleNamespace(position_manager="0x" + "11" * 20))
    monkeypatch.setattr(H.R, "view", lambda rpc, to, sig, args: 0)
    assert H.v4_held_rows(None, None, "c", "0x" + "aa" * 20) == []

    def down(*a):
        raise H.R.LpReadError("node down")
    monkeypatch.setattr(H.R, "view", down)
    import pytest
    with pytest.raises(H.R.LpReadError):
        H.v4_held_rows(None, None, "c", "0x" + "aa" * 20)
