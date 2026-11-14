"""117: the guard judges a v4 SEED — initialise a pool and mint the first
position in ONE transaction — from the signed bytes, and each rule has its
refusing twin."""
import time
from types import SimpleNamespace

import pytest

from core.wallet import abi, dex_registry, liquidity_guard as G, tx_guard, univ3_math as M
from tools.defi import lp_v4 as V

TKN = "0x5555555555555555555555555555555555555555"
ZERO = "0x" + "0" * 40
HOLDER = "0x" + "ab" * 20
ROW = dex_registry.row_for("robinhood", "v4")
NPM, PM, P2 = ROW.position_manager, ROW.pool_manager, ROW.permit2
KEY = {"currency0": ZERO, "currency1": TKN, "fee": 10000, "tickSpacing": 200, "hooks": ZERO}
PID = G.v4_pool_id((ZERO, TKN, 10000, 200, ZERO))
LO, HI = -160000, 380000
SQRT = M.sqrt_price_at_tick(156800)          # about 6.4 M token per native
LIQ = 5 * 10 ** 21
NEED0, NEED1 = M.amounts_for_liquidity(SQRT, M.sqrt_price_at_tick(LO), M.sqrt_price_at_tick(HI), LIQ)
MAX0, MAX1 = NEED0 * 101 // 100, NEED1 * 101 // 100


def _mint(key=KEY, lo=LO, hi=HI, liq=LIQ, a0=MAX0, a1=MAX1, owner=HOLDER):
    unlock = V.encode_mint_unlock(key, lo, hi, liq, a0, a1, owner)
    return V.encode_modify_liquidities(unlock, int(time.time()) + 120)


def _seed_tx(sqrt=SQRT, init_key=KEY, value=MAX0, **mint):
    return {"to": NPM, "chainId": 4663, "value": value,
            "data": V.encode_seed_multicall(init_key, sqrt, _mint(**mint))}


def _plain_tx(value=MAX0, **mint):
    return {"to": NPM, "chainId": 4663, "value": value, "data": _mint(**mint)}


def _intent(seed=True, **kw):
    events = ((PM, V.TOPIC_MODIFY_LIQUIDITY),) + (((PM, G.TOPIC_V4_INITIALIZE),) if seed else ())
    base = dict(chain="robinhood", token=None, to=NPM, amount_raw=0, max_spend_usd=100.0,
                is_liquidity_op=True, lp_outflows=((None, MAX0), (TKN, MAX1)),
                watch_spenders=(P2,), lp_position=(NPM, None), lp_position_effect="mint",
                expected_events=events)
    base.update(kw)
    return tx_guard.TxIntent(**base)


# -- the shape ---------------------------------------------------------------

def test_the_seed_shape_passes_and_carries_the_signed_price():
    G.structural(_intent(), _seed_tx(), holder=HOLDER)
    m = G.decode_v4_mint(_seed_tx())
    assert (m["seed_sqrt_price"], m["pool_id"], m["tick_lower"], m["tick_upper"]) == (SQRT, PID, LO, HI)
    assert G.decode_v4_mint(_plain_tx())["seed_sqrt_price"] is None


def test_a_bare_initialize_pool_refuses():
    init = abi.encode_call(V.POSM_INITIALIZE_POOL["name"], V.POSM_INITIALIZE_POOL["inputs"],
                           [V._key_tuple(KEY), SQRT])
    with pytest.raises(ValueError, match="bare initializePool"):
        G.structural(_intent(), {"to": NPM, "chainId": 4663, "value": 0, "data": init}, holder=HOLDER)


def _multicall(calls):
    return abi.encode_call("multicall", [{"type": "bytes[]"}], [[bytes.fromhex(c[2:]) for c in calls]])


def test_any_other_multicall_refuses():
    init = abi.encode_call(V.POSM_INITIALIZE_POOL["name"], V.POSM_INITIALIZE_POOL["inputs"],
                           [V._key_tuple(KEY), SQRT])
    for calls in ([init, _mint(), _mint()], [_mint(), init], [_mint()], [init]):
        with pytest.raises(ValueError, match="exactly multicall"):
            G.structural(_intent(), {"to": NPM, "chainId": 4663, "value": MAX0,
                                     "data": _multicall(calls)}, holder=HOLDER)


def test_the_two_keys_must_be_equal():
    with pytest.raises(ValueError, match="same PoolKey"):
        G.structural(_intent(), _seed_tx(init_key=dict(KEY, fee=3000)), holder=HOLDER)


def test_a_dynamic_fee_key_refuses():
    key = dict(KEY, fee=G.V4_DYNAMIC_FEE_FLAG)
    with pytest.raises(ValueError, match="static fee"):
        G.structural(_intent(), _seed_tx(init_key=key, key=key), holder=HOLDER)


@pytest.mark.parametrize("lo,hi", [(-160001, 380000), (-160000, 380100), (380000, -160000),
                                   (-887400, 380000)])
def test_ticks_must_be_ordered_aligned_and_in_bounds(lo, hi):
    with pytest.raises(ValueError, match="ticks"):
        G.structural(_intent(), _seed_tx(lo=lo, hi=hi), holder=HOLDER)


def test_the_position_is_minted_to_the_holder_only():
    with pytest.raises(ValueError, match="not the holder"):
        G.structural(_intent(), _seed_tx(owner="0x" + "cd" * 20), holder=HOLDER)


def test_value_must_equal_the_native_leg():
    with pytest.raises(ValueError, match="tx.value"):
        G.structural(_intent(), _seed_tx(value=MAX0 - 1), holder=HOLDER)


def test_an_unpinned_hook_refuses():
    key = dict(KEY, hooks="0x" + "1" * 40)
    with pytest.raises(ValueError, match="no pinned code hash"):
        G.structural(_intent(), _seed_tx(init_key=key, key=key), holder=HOLDER)


# -- the price bound is in the signed bytes -----------------------------------

def test_maxima_inside_the_gap_pass_and_wider_ones_refuse():
    edge = lambda need: need * (10000 + G.SEED_MAX_GAP_BPS) // 10000
    G.structural(_intent(lp_outflows=((None, edge(NEED0)), (TKN, edge(NEED1)))),
                 _seed_tx(a0=edge(NEED0), a1=edge(NEED1), value=edge(NEED0)), holder=HOLDER)
    for a0, a1, name in ((edge(NEED0) + 2, MAX1, "amount0"), (MAX0, edge(NEED1) + 2, "amount1")):
        with pytest.raises(ValueError, match=f"{name} maximum"):
            G.structural(_intent(lp_outflows=((None, a0), (TKN, a1))),
                         _seed_tx(a0=a0, a1=a1, value=a0), holder=HOLDER)


def test_a_price_far_from_the_maxima_refuses():
    # the same maxima signed with twice the price: the native leg needs far less
    with pytest.raises(ValueError, match="price is not bounded"):
        G.structural(_intent(), _seed_tx(sqrt=SQRT * 2), holder=HOLDER)


def test_initialize_is_required_by_a_seed_and_by_nothing_else():
    with pytest.raises(ValueError, match="Initialize event"):
        G.structural(_intent(seed=False), _seed_tx(), holder=HOLDER)
    with pytest.raises(ValueError, match="Initialize event"):
        G.structural(_intent(seed=True), _plain_tx(), holder=HOLDER)
    G.structural(_intent(seed=False), _plain_tx(), holder=HOLDER)


# -- the pool's own state (the pin step) --------------------------------------

def _rpc(sqrt_now):
    def rpc(method, params, timeout=8.0):
        assert method == "eth_call" and params[0]["to"].lower() == PM.lower()
        return "0x" + format(sqrt_now | (156800 << 160), "064x")
    return rpc


@pytest.fixture
def pins_ok(monkeypatch):
    monkeypatch.setattr(dex_registry, "verify_pins", lambda *a, **k: None)
    monkeypatch.setattr(dex_registry, "verify_hook", lambda *a, **k: None)


def test_a_seed_needs_a_pool_with_no_price(pins_ok):
    G.verify_pins(_rpc(0), _intent(), _seed_tx())
    with pytest.raises(ValueError, match="already initialised"):
        G.verify_pins(_rpc(SQRT), _intent(), _seed_tx())


def test_a_plain_mint_needs_an_initialised_pool_and_maxima_near_its_price(pins_ok):
    G.verify_pins(_rpc(SQRT), _intent(seed=False), _plain_tx())
    with pytest.raises(ValueError, match="not initialised"):
        G.verify_pins(_rpc(0), _intent(seed=False), _plain_tx())
    with pytest.raises(ValueError, match="price is not bounded"):
        G.verify_pins(_rpc(SQRT * 2), _intent(seed=False), _plain_tx())


def test_an_unreadable_pool_is_not_an_empty_one(pins_ok):
    with pytest.raises(ValueError, match="returned nothing"):
        G.verify_pins(lambda *a, **k: "0x", _intent(), _seed_tx())


# -- the events equal the signed bytes ----------------------------------------

def _pad(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def _modify(salt=7, pid=PID, lo=LO, hi=HI, delta=LIQ):
    i = lambda n: n.to_bytes(32, "big", signed=True)
    return {"address": PM, "topics": [V.TOPIC_MODIFY_LIQUIDITY, pid, _pad(NPM)],
            "data": "0x" + (i(lo) + i(hi) + i(delta) + salt.to_bytes(32, "big")).hex()}


def _init(pid=PID, sqrt=SQRT):
    w = lambda n: n.to_bytes(32, "big")
    return {"address": PM, "topics": [G.TOPIC_V4_INITIALIZE, pid, _pad(ZERO), _pad(TKN)],
            "data": "0x" + (w(10000) + w(200) + w(0) + w(sqrt) + w(156800)).hex()}


def test_the_seed_events_must_equal_the_signed_bytes():
    G.assert_position_events(_intent(), [_init(), _modify()], 7, _seed_tx())
    other = "0x" + "9" * 64
    for logs, why in (([_init(), _modify(pid=other)], "pool other than"),
                      ([_init(), _modify(lo=LO + 200)], "ticks other than"),
                      ([_init(), _modify(delta=LIQ - 1)], "liquidity other than"),
                      ([_init(sqrt=SQRT + 1), _modify()], "Initialize event"),
                      ([_init(pid=other), _modify()], "Initialize event"),
                      ([_modify()], "Initialize event"),
                      ([_init(), _init(), _modify()], "more than one pool")):
        with pytest.raises(ValueError, match=why):
            G.assert_position_events(_intent(), logs, 7, _seed_tx())


def test_a_plain_mint_may_not_initialise_a_pool():
    G.assert_position_events(_intent(seed=False), [_modify()], 7, _plain_tx())
    with pytest.raises(ValueError, match="may not initialise"):
        G.assert_position_events(_intent(seed=False), [_init(), _modify()], 7, _plain_tx())


# -- the value of a pool this transaction creates ------------------------------

def _deltas(used0, used1, logs):
    return SimpleNamespace(token_deltas={TKN: -used1}, native_delta=-used0, logs=logs)


def _price(native=None, token=None):
    wrapped = __import__("core.wallet.chains", fromlist=["x"]).get("robinhood").wrapped_native
    return lambda chain, asset: native if asset.lower() == wrapped.lower() else token


def test_creates_pool_knows_the_v4_initialize_event():
    assert G.creates_pool(_intent()) and not G.creates_pool(_intent(seed=False))


def test_an_unpriced_token_leg_is_valued_through_the_signed_price():
    usd, implied = G.price_outflows(_intent(), _deltas(NEED0, NEED1, [_init(), _modify()]),
                                    _price(native=3500.0), None, lambda chain, token: 18)
    native_usd = NEED0 / 1e18 * 3500.0
    token_as_native = NEED1 * 2 ** 192 / SQRT ** 2 / 1e18 * 3500.0
    assert implied and usd == pytest.approx(native_usd + token_as_native)


def test_a_priced_token_leg_takes_the_larger_value():
    own = NEED1 / 1e18 * 1.0        # an own price far above the pool's
    usd, implied = G.price_outflows(_intent(), _deltas(NEED0, NEED1, [_init(), _modify()]),
                                    _price(native=3500.0, token=1.0), None, lambda chain, token: 18)
    assert not implied and usd == pytest.approx(NEED0 / 1e18 * 3500.0 + own)


def test_a_low_own_price_does_not_lower_the_value():
    usd, _ = G.price_outflows(_intent(), _deltas(NEED0, NEED1, [_init(), _modify()]),
                              _price(native=3500.0, token=1e-12), None, lambda chain, token: 18)
    assert usd == pytest.approx(NEED0 / 1e18 * 3500.0 + NEED1 * 2 ** 192 / SQRT ** 2 / 1e18 * 3500.0)


def test_a_seed_with_no_priced_native_leg_refuses():
    with pytest.raises(ValueError, match="native leg"):
        G.price_outflows(_intent(), _deltas(NEED0, NEED1, [_init(), _modify()]),
                         _price(token=1.0), None, lambda chain, token: 18)
    with pytest.raises(ValueError, match="native leg"):
        G.price_outflows(_intent(), _deltas(NEED0, NEED1, [_init(), _modify()]),
                         _price(), None, lambda chain, token: 18)


def test_an_existing_pool_still_refuses_the_paired_valuation():
    with pytest.raises(ValueError, match="already exists"):
        G.price_outflows(_intent(seed=False), _deltas(NEED0, NEED1, [_modify()]),
                         _price(native=3500.0), None, lambda chain, token: 18)
