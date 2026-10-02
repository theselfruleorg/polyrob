"""048 phase 3 / core handoff W6: the v4 reads on the Pons PoolKey.

Pins the PoolKey question the 2026-09-29 core evaluation raised ("GeckoTerminal
names the pool PNL/WETH, the key uses native address(0)"): the NATIVE key
hashes to the pool id the mint contract buys from; a WETH key does not.
"""
import asyncio

import pytest

from core.wallet import abi, dex_registry
from tools.defi import lp_reads, lp_v4 as V
from tools.defi.data_tool import DefiDataTool, LpPoolInfoParams, LpQuoteParams

PNL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
PINNED_POOL_ID = "0x43b7b259007600ad19df15a23b620fd77c8c121c8ecc8788d5116a833d031e94"
HOOK = "0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044"
WETH_4663 = "0x7943e237c7F95DA44E0301572D358911207852Fa"
# Measured on a 4663 fork 2026-09-29 (StateView.getSlot0 / getLiquidity).
SQRT = 627219137286278685928990650320595
TICK = 179543
LIQ = 29277002188455997052109

RECORD = {"token": PNL, "pairToken": "0x" + "0" * 40, "poolFee": 0,
          "tickSpacing": 200, "phase": 2, "exists": True}


def test_native_pons_key_is_the_pinned_pool_id():
    key = lp_reads.pons_pool_key(RECORD)
    assert key["currency0"] == "0x" + "0" * 40
    assert key["currency1"].lower() == PNL.lower()
    assert (key["fee"], key["tickSpacing"], key["hooks"]) == (0, 200, HOOK)
    assert lp_reads.pool_id(key) == PINNED_POOL_ID


def test_a_weth_key_is_a_different_pool():
    key = lp_reads.pons_pool_key(dict(RECORD, pairToken=WETH_4663))
    assert lp_reads.pool_id(key) != PINNED_POOL_ID


def _word(sqrt, tick, pfee=0, lfee=0):
    return sqrt | ((tick & 0xFFFFFF) << 160) | (pfee << 184) | (lfee << 208)


def test_slot0_word_decodes_the_measured_values():
    word = _word(SQRT, TICK)
    # The handoff quoted the word as 0x…02bd57…1eec9e562f795c4786ef4332c6d3.
    assert hex(word).endswith("2bd57" + format(SQRT, "040x"))
    assert format(SQRT, "x").endswith("1eec9e562f795c4786ef4332c6d3")
    assert V.decode_slot0_word(word) == (SQRT, TICK, 0, 0)
    assert V.decode_slot0_word(_word(SQRT, -200, 3, 7)) == (SQRT, -200, 3, 7)


class _Rpc:
    """eth_call answers keyed by (to, selector); extsload keyed by slot."""
    def __init__(self):
        self.calls, self.slots = {}, {}

    def add(self, to, spec, values):
        sel = abi.selector(abi.signature_of(spec["name"], spec["inputs"]))
        self.calls[(to.lower(), sel)] = "0x" + abi.encode(spec["outputs"], values).hex()

    def __call__(self, method, params, timeout=8.0):
        assert method == "eth_call"
        data = params[0]["data"]
        to = params[0]["to"].lower()
        if data.startswith(abi.selector("extsload(bytes32)")):
            return "0x" + format(self.slots[int(data[10:74], 16)], "064x")
        return self.calls[(to, data[:10])]


def _state_view_rpc():
    row = dex_registry.row_for("robinhood", "v4")
    rpc = _Rpc()
    rpc.add(row.state_view, V.STATE_VIEW_GET_SLOT0, [SQRT, TICK, 0, 0])
    rpc.add(row.state_view, V.STATE_VIEW_GET_LIQUIDITY, [LIQ])
    return rpc


def test_pool_state_reads_state_view():
    st = V.pool_state(_state_view_rpc(), "robinhood", PINNED_POOL_ID)
    assert (st.sqrt_price_x96, st.tick, st.liquidity, st.source) == (SQRT, TICK, LIQ, "state_view")


def test_pool_state_falls_back_to_extsload(monkeypatch):
    import dataclasses
    row = dex_registry.row_for("robinhood", "v4")
    monkeypatch.setitem(dex_registry._ROWS, ("robinhood", "v4"),
                        dataclasses.replace(row, state_view=None))
    rpc = _Rpc()
    slot = V._state_slot(PINNED_POOL_ID)
    rpc.slots[slot] = _word(SQRT, TICK)
    rpc.slots[slot + 3] = LIQ
    st = V.pool_state(rpc, "robinhood", PINNED_POOL_ID)
    assert (st.sqrt_price_x96, st.tick, st.liquidity, st.source) == (SQRT, TICK, LIQ, "extsload")


def test_unreadable_state_raises_not_zero():
    with pytest.raises(lp_reads.LpReadError):
        V.pool_state(lambda m, p, timeout=8.0: "0x", "robinhood", PINNED_POOL_ID)


def test_mint_unlock_is_mint_settle_pair_sweep():
    key = lp_reads.pons_pool_key(RECORD)
    owner = "0x" + "ab" * 20
    unlock = V.encode_mint_unlock(key, -887200, 887200, 10 ** 20, 10 ** 16, 10 ** 24, owner)
    actions, params = abi.decode([{"type": "bytes"}, {"type": "bytes[]"}], unlock)
    as_bytes = lambda v: v if isinstance(v, (bytes, bytearray)) else bytes.fromhex(v[2:])
    assert as_bytes(actions) == bytes([0x02, 0x0D, 0x14])
    assert len(params) == 3
    sweep = abi.decode([{"type": "address"}, {"type": "address"}], as_bytes(params[2]))
    assert sweep[0].lower() == key["currency0"] and sweep[1].lower() == owner


def test_modify_liquidity_topic_is_the_event_signature():
    assert V.topic_modify_liquidity() == V.TOPIC_MODIFY_LIQUIDITY


def test_full_range_quote_uses_both_bounds():
    lo, hi, liq, u0, u1 = V.full_range_quote(SQRT, 200, 10 ** 18, V.MAX_UINT128)
    assert (lo, hi) == (-887200, 887200)
    assert 0 < u0 <= 10 ** 18 and u1 > 0 and liq > 0


def _tool(monkeypatch):
    monkeypatch.setattr(V, "pons_key_for", lambda rpc, token: lp_reads.pons_pool_key(RECORD))
    return DefiDataTool(lp_rpc=_state_view_rpc(), holder=None)


def test_lp_pool_info_v4_reads_the_pons_pool(monkeypatch):
    res = asyncio.run(_tool(monkeypatch).lp_pool_info(
        LpPoolInfoParams(protocol="v4", token_a="native", token_b=PNL)))
    assert PINNED_POOL_ID in res.extracted_content
    assert "tick: 179543" in res.extracted_content


def test_lp_quote_v4_is_full_range_only(monkeypatch):
    tool = _tool(monkeypatch)
    res = asyncio.run(tool.lp_quote(LpQuoteParams(
        protocol="v4", token_a="native", token_b=PNL, amount_a=1.0, range="1,2")))
    assert "full range only" in res.error


def test_lp_quote_v4_refuses_a_pair_that_is_not_the_key(monkeypatch):
    tool = _tool(monkeypatch)
    res = asyncio.run(tool.lp_quote(LpQuoteParams(
        protocol="v4", token_a=WETH_4663, token_b=PNL, amount_a=1.0)))
    assert "not this token's Pons PoolKey" in res.error


def test_v4_pins_cover_state_view_permit2_and_the_hook():
    row = dex_registry.row_for("robinhood", "v4")
    for addr in (row.state_view, row.permit2):
        assert ("robinhood", addr.lower()) in dex_registry.CODE_HASHES
    assert dex_registry.hook_pinned("robinhood", HOOK)
    assert dex_registry.hook_pinned("robinhood", "0x" + "0" * 40)
    assert not dex_registry.hook_pinned("robinhood", "0x" + "1" * 40)


def test_verify_pins_v4_hashes_state_view_and_permit2():
    seen = []

    def rpc(method, params):
        seen.append(params[0].lower())
        return "0x6001600155"
    with pytest.raises(dex_registry.DexPinError):
        dex_registry.verify_pins(rpc, "robinhood", "v4")
    row = dex_registry.row_for("robinhood", "v4")
    # First mismatch refuses; walk the order with a hash-matching rpc instead.
    from eth_utils import keccak
    code = {a: "0x60" + format(i, "02x") for i, a in enumerate(
        (row.position_manager.lower(), row.pool_manager.lower(),
         row.state_view.lower(), row.permit2.lower()))}
    pinned = {("robinhood", a): "0x" + keccak(bytes.fromhex(c[2:])).hex() for a, c in code.items()}
    seen.clear()
    orig = dict(dex_registry.CODE_HASHES)
    try:
        dex_registry.CODE_HASHES.update(pinned)
        dex_registry.verify_pins(lambda m, p: (seen.append(p[0].lower()) or code[p[0].lower()]),
                                 "robinhood", "v4")
    finally:
        dex_registry.CODE_HASHES.clear()
        dex_registry.CODE_HASHES.update(orig)
    assert set(seen) == set(code)


def test_verify_hook_refuses_unpinned_and_changed():
    with pytest.raises(dex_registry.DexPinError, match="no measured code hash"):
        dex_registry.verify_hook(lambda m, p: "0x60", "robinhood", "0x" + "1" * 40)
    with pytest.raises(dex_registry.DexPinError, match="hashes to"):
        dex_registry.verify_hook(lambda m, p: "0x60", "robinhood", HOOK)
    dex_registry.verify_hook(lambda m, p: 1 / 0, "robinhood", "0x" + "0" * 40)
