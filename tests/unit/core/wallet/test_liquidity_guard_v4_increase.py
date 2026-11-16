"""118 W6: the v4 increase — INCREASE_LIQUIDITY + SETTLE_PAIR + SWEEP(holder) on a
position the wallet holds — judged from the signed bytes and the pinned
PositionManager's own record of the position."""
from types import SimpleNamespace

import pytest

from core.wallet import abi, dex_registry, liquidity_guard as G, tx_guard, univ3_math as M
from tools.defi import lp_v4 as V

ROW = dex_registry.row_for("robinhood", "v4")
NPM, PM, P2 = ROW.position_manager.lower(), ROW.pool_manager.lower(), ROW.permit2
ZERO = "0x" + "0" * 40
TKN = "0x" + "55" * 20
HOLDER = "0x" + "ab" * 20
POS, LO, HI = 42, -170000, 400000
SQRT = M.sqrt_price_at_tick(160000)
LIQ = 10 ** 19
NEED = M.amounts_for_liquidity(SQRT, M.sqrt_price_at_tick(LO), M.sqrt_price_at_tick(HI), LIQ)
MAX0, MAX1 = NEED[0] * 101 // 100, NEED[1] * 101 // 100


def _tx(token_id=POS, liq=LIQ, a0=MAX0, a1=MAX1, c0=ZERO, c1=TKN, to=HOLDER, value=None,
        unlock=None):
    unlock = unlock or V.encode_increase_unlock(token_id, liq, a0, a1, c0, c1, to)
    return {"to": NPM, "chainId": 4663, "value": a0 if value is None else value,
            "data": V.encode_modify_liquidities(unlock, 2 ** 40)}


def _intent(**kw):
    base = dict(chain="robinhood", token=None, to=NPM, amount_raw=0, max_spend_usd=100.0,
                is_liquidity_op=True, lp_outflows=((None, MAX0), (TKN, MAX1)),
                watch_spenders=(P2,), lp_position=(NPM, POS), lp_position_effect="hold",
                expected_events=((PM, V.TOPIC_MODIFY_LIQUIDITY),))
    base.update(kw)
    return tx_guard.TxIntent(**base)


def test_the_increase_shape_passes_and_is_not_a_collect():
    intent = _intent()
    assert G.is_v4_increase(intent) and not G.is_v4_collect(intent)
    assert not G.may_ride_account(intent)
    G.structural(intent, _tx(), holder=HOLDER)
    c = G.decode_v4_increase(_tx())
    assert (c["token_id"], c["liquidity"], c["recipient"]) == (POS, LIQ, HOLDER)


@pytest.mark.parametrize("tx_kw,intent_kw,why", [
    (dict(token_id=POS + 1), {}, "names position"),
    (dict(to="0x" + "77" * 20), {}, "SWEEP pays"),
    (dict(a1=MAX1 + 1), {}, "may pay"),
    (dict(value=MAX0 - 1), {}, "tx.value"),
    (dict(liq=0), {}, "must add liquidity"),
    (dict(c0=TKN, c1=ZERO), {}, "sorted"),
    ({}, dict(expected_events=()), "ModifyLiquidity event"),
    ({}, dict(lp_outflows=((None, MAX0), (TKN, MAX1), ("0x" + "66" * 20, 1))), "at most two"),
    ({}, dict(via_account="0x" + "ac" * 20), "only to collect fees"),
])
def test_what_refuses(tx_kw, intent_kw, why):
    with pytest.raises(ValueError, match=why):
        G.structural(_intent(**intent_kw), _tx(**tx_kw), holder=HOLDER)


def test_another_action_sequence_and_hook_data_refuse():
    inc = abi.encode(G._DECREASE_PARAMS, [POS, LIQ, MAX0, MAX1, b"\x01"])
    pair = abi.encode(G._PAIR, [ZERO, TKN])
    sweep = abi.encode(G._PAIR, [ZERO, HOLDER])
    wrap = lambda actions, params: abi.encode([{"type": "bytes"}, {"type": "bytes[]"}],
                                              [bytes(actions), params])
    with pytest.raises(ValueError, match="hookData must be empty"):
        G.structural(_intent(), _tx(unlock=wrap([0, 0x0D, 0x14], [inc, pair, sweep])), holder=HOLDER)
    clean = abi.encode(G._DECREASE_PARAMS, [POS, LIQ, MAX0, MAX1, b""])
    with pytest.raises(ValueError, match="exactly INCREASE_LIQUIDITY"):
        G.structural(_intent(), _tx(unlock=wrap([0, 0x0D], [clean, pair])), holder=HOLDER)
    with pytest.raises(ValueError, match="SWEEP must return currency0"):
        G.structural(_intent(), _tx(unlock=wrap(
            [0, 0x0D, 0x14], [clean, pair, abi.encode(G._PAIR, [TKN, HOLDER])])), holder=HOLDER)


def _chain(monkeypatch, *, key=None, lo=LO, hi=HI, sqrt=SQRT, pinned=True):
    key = key or {"currency0": ZERO, "currency1": TKN, "fee": 10000, "tickSpacing": 200,
                  "hooks": ZERO}
    monkeypatch.setattr(dex_registry, "verify_pins", lambda *a: None)
    monkeypatch.setattr(dex_registry, "verify_hook", lambda *a: None)
    monkeypatch.setattr(dex_registry, "hook_pinned", lambda chain, hook: pinned)
    monkeypatch.setattr(G, "read_v4_position_range", lambda *a: (key, lo, hi))
    monkeypatch.setattr(G, "read_v4_sqrt_price", lambda *a: sqrt)


def test_the_chain_half_binds_the_pair_the_hook_and_the_price(monkeypatch):
    _chain(monkeypatch)
    G.verify_pins(None, _intent(), _tx())
    _chain(monkeypatch, key={"currency0": ZERO, "currency1": "0x" + "66" * 20, "fee": 10000,
                             "tickSpacing": 200, "hooks": ZERO})
    with pytest.raises(ValueError, match="but position #42 is on"):
        G.verify_pins(None, _intent(), _tx())
    _chain(monkeypatch, pinned=False)
    with pytest.raises(ValueError, match="no pinned code hash"):
        G.verify_pins(None, _intent(), _tx())
    _chain(monkeypatch, sqrt=0)
    with pytest.raises(ValueError, match="not initialised"):
        G.verify_pins(None, _intent(), _tx())
    # A maximum far above what the signed liquidity needs at the pool price is unbounded.
    _chain(monkeypatch)
    with pytest.raises(ValueError, match="price is not bounded"):
        G.verify_pins(None, _intent(lp_outflows=((None, MAX0), (TKN, MAX1 * 2))), _tx(a1=MAX1 * 2))
    # The bound is over the position's OWN range, not one the caller names.
    _chain(monkeypatch, lo=150000, hi=170000)
    with pytest.raises(ValueError, match="price is not bounded"):
        G.verify_pins(None, _intent(), _tx())


def test_position_info_word_decodes_signed_ticks():
    info = (1 << 56) | ((HI & 0xFFFFFF) << 32) | ((LO & 0xFFFFFF) << 8)
    raw = "0x" + abi.encode(G.POSM_POOL_AND_POSITION_INFO["outputs"],
                            [(ZERO, TKN, 10000, 200, ZERO), info]).hex()
    key, lo, hi = G.read_v4_position_range(lambda m, p: raw, "robinhood", POS)
    assert (lo, hi) == (LO, HI) and key["currency1"] == TKN


def _event(delta, salt=POS, sender=NPM):
    data = b"".join(n.to_bytes(32, "big", signed=True) for n in (LO, HI, delta)) + salt.to_bytes(32, "big")
    return {"address": PM, "topics": [V.TOPIC_MODIFY_LIQUIDITY, "0x" + "ab" * 32,
                                      "0x" + "0" * 24 + sender[2:]], "data": "0x" + data.hex()}


def test_the_event_adds_exactly_the_signed_liquidity_to_our_position():
    intent, tx = _intent(), _tx()
    G.assert_position_events(intent, [_event(LIQ)], POS, tx)
    for logs, why in (([_event(LIQ - 1)], "exactly one ModifyLiquidity"),
                      ([_event(LIQ), _event(LIQ)], "expected exactly one"),
                      ([_event(-LIQ)], "may not withdraw"),
                      ([_event(LIQ, salt=POS + 1)], "different position id"),
                      ([], "expected exactly one")):
        with pytest.raises(ValueError, match=why):
            G.assert_position_events(intent, logs, POS, tx)
