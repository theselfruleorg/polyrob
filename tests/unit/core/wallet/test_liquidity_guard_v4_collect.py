"""115 W4: the v4 fee collect — DECREASE_LIQUIDITY(0) + TAKE_PAIR(holder) — judged by the
ONE authorizer, from the wallet and through a token-bound account the wallet owns.

The shape is the contract repo's ``test_fork_tradesPayTheAccount``: actions ``0x01 0x11``,
``(tokenId, 0, 0, 0, "")`` and ``(currency0, currency1, recipient)``."""
import dataclasses
import subprocess

import pytest

from core.wallet import abi, dex_registry, erc6551, liquidity_guard as G, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tools.defi import lp_v4 as V

ROW = dex_registry.row_for("robinhood", "v4")
NPM, PM = ROW.position_manager.lower(), ROW.pool_manager.lower()
ZERO = "0x" + "0" * 40
TKN = "0x" + "55" * 20
HOLDER = "0x" + "ab" * 20          # the wallet
ACCT = "0x" + "ac" * 20            # an NFT's account the wallet owns
OTHER = "0x" + "77" * 20
POS = 42
KEY = {"currency0": ZERO, "currency1": TKN, "fee": 10000, "tickSpacing": 200, "hooks": ZERO}


def _data(token_id=POS, c0=ZERO, c1=TKN, to=HOLDER, unlock=None):
    unlock = unlock if unlock is not None else V.encode_collect_unlock(token_id, c0, c1, to)
    return V.encode_modify_liquidities(unlock, 2 ** 40)


def _tx(**kw):
    value = kw.pop("value", 0)
    target = kw.pop("target", NPM)
    return {"to": target, "chainId": 4663, "value": value, "maxFeePerGas": 10 ** 9,
            "data": _data(**kw)}


def _intent(**kw):
    base = dict(chain="robinhood", token=None, to=NPM, amount_raw=0, max_spend_usd=5.0,
                is_liquidity_op=True, lp_inflows=((None, 0), (TKN, 0)),
                lp_position=(NPM, POS), lp_position_effect="hold")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _unlock(actions, params):
    return abi.encode([{"type": "bytes"}, {"type": "bytes[]"}], [bytes(actions), params])


def _decrease(liquidity=0, min0=0, min1=0, hook=b"", token_id=POS):
    return abi.encode([{"type": "uint256"}, {"type": "uint256"}, {"type": "uint128"},
                       {"type": "uint128"}, {"type": "bytes"}],
                      [token_id, liquidity, min0, min1, hook])


def _take(c0=ZERO, c1=TKN, to=HOLDER):
    return abi.encode([{"type": "address"}] * 3, [c0, c1, to])


# -- the constants, computed ---------------------------------------------------

def _cast_sig(sig):
    try:
        return subprocess.run(["cast", "sig", sig], capture_output=True, text=True,
                              timeout=20).stdout.strip() or None
    except Exception:  # noqa: BLE001 — cast is optional on a CI box
        return None


@pytest.mark.parametrize("sig,literal", [
    ("modifyLiquidities(bytes,uint256)", "0xdd46508f"),
    ("getPoolAndPositionInfo(uint256)", "0x7ba03aad"),
])
def test_every_selector_is_computed_from_its_signature(sig, literal):
    assert abi.selector(sig) == literal
    cast = _cast_sig(sig)
    if cast is not None:
        assert cast == literal
    spec = G.POSM_POOL_AND_POSITION_INFO
    assert abi.selector(abi.signature_of(spec["name"], spec["inputs"])) == "0x7ba03aad"


def test_the_action_ids_are_the_v4_periphery_ones():
    # Actions.sol, and the contract repo's fork test: abi.encodePacked(uint8(0x01), uint8(0x11)).
    assert (V.ACTION_DECREASE_LIQUIDITY, V.ACTION_TAKE_PAIR) == (0x01, 0x11)
    assert G._V4_COLLECT_SHAPE == bytes([V.ACTION_DECREASE_LIQUIDITY, V.ACTION_TAKE_PAIR])
    assert G.TOPIC_V4_MODIFY_LIQUIDITY == V.topic_modify_liquidity()


# -- the encoder ---------------------------------------------------------------

def test_encoder_bytes_round_trip():
    unlock = V.encode_collect_unlock(POS, ZERO, TKN, ACCT)
    actions, params = abi.decode([{"type": "bytes"}, {"type": "bytes[]"}], unlock)
    assert G._raw(actions) == b"\x01\x11" and len(params) == 2
    assert G._raw(params[0]) == _decrease()
    assert G._raw(params[1]) == _take(to=ACCT)
    data = V.encode_modify_liquidities(unlock, 1234)
    assert data.startswith("0xdd46508f")
    assert G.decode_v4_collect({"data": data}) == {
        "token_id": POS, "currency0": ZERO, "currency1": TKN, "recipient": ACCT,
        "liquidity": 0, "min0": 0, "min1": 0}


# -- the shape the guard allows --------------------------------------------------

def test_the_exact_shape_passes():
    G.structural(_intent(), _tx(), holder=HOLDER)
    assert G.is_v4_collect(_intent()) and G.may_ride_account(_intent())


@pytest.mark.parametrize("liquidity", [1, 10 ** 18])
def test_a_non_zero_decrease_is_a_withdrawal_and_refuses(liquidity):
    unlock = _unlock([0x01, 0x11], [_decrease(liquidity=liquidity), _take()])
    with pytest.raises(ValueError, match="withdrawal"):
        G.structural(_intent(), _tx(unlock=unlock), holder=HOLDER)


@pytest.mark.parametrize("actions,params", [
    ([0x01, 0x11, 0x14], "three"),            # a third action (SWEEP)
    ([0x01], "one"),                          # no TAKE_PAIR
    ([0x11, 0x01], "two"),                    # the reverse order
    ([0x03, 0x11], "two"),                    # BURN_POSITION in place of the decrease
])
def test_any_other_action_sequence_refuses(actions, params):
    p = {"one": [_decrease()], "two": [_decrease(), _take()],
         "three": [_decrease(), _take(), _take()]}[params]
    with pytest.raises(ValueError, match="exactly DECREASE_LIQUIDITY \\+ TAKE_PAIR"):
        G.structural(_intent(), _tx(unlock=_unlock(actions, p)), holder=HOLDER)


def test_a_recipient_that_is_not_the_holder_refuses():
    with pytest.raises(ValueError, match="not the holder"):
        G.structural(_intent(), _tx(to=OTHER), holder=HOLDER)
    # through an account, the holder IS the account: paying the wallet refuses too
    with pytest.raises(ValueError, match="not the holder"):
        G.structural(_intent(), _tx(to=HOLDER), holder=ACCT)


def test_minimums_hook_data_position_and_value_are_exact():
    for d, match in ((_decrease(min0=1), "minimums"), (_decrease(hook=b"\x01"), "hookData"),
                     (_decrease(token_id=POS + 1), "names position")):
        with pytest.raises(ValueError, match=match):
            G.structural(_intent(), _tx(unlock=_unlock([0x01, 0x11], [d, _take()])),
                         holder=HOLDER)
    with pytest.raises(ValueError, match="sends no value"):
        G.structural(_intent(), _tx(value=1), holder=HOLDER)
    with pytest.raises(ValueError, match="know the holder"):
        G.structural(_intent(), _tx(), holder=None)


def test_the_declared_inflows_are_exactly_the_taken_currencies():
    for inflows in (((None, 0),), ((None, 0), (OTHER, 0)), ((None, 0), (TKN, -1))):
        with pytest.raises(ValueError):
            G.structural(_intent(lp_inflows=inflows), _tx(), holder=HOLDER)


def test_a_target_that_is_not_the_pinned_position_manager_refuses():
    with pytest.raises(ValueError, match="pinned v4 position manager"):
        G.structural(_intent(), _tx(target=OTHER), holder=HOLDER)
    # an intent aimed elsewhere is judged as v3 and refused by the v3 decode
    with pytest.raises(ValueError):
        G.structural(_intent(to=OTHER), _tx(target=OTHER), holder=HOLDER)


def test_v4_withdrawal_and_mint_through_an_account_still_refuse():
    w = _intent(lp_position_effect="burn")
    with pytest.raises(ValueError, match="MINT only"):
        G.structural(w, _tx(), holder=HOLDER)
    assert not G.may_ride_account(w)
    assert not G.may_ride_account(_intent(lp_outflows=((None, 1),)))
    v3 = _intent(to=dex_registry.row_for("base", "v3").position_manager, chain="base")
    assert not G.may_ride_account(v3)


# -- the PoolKey read (verify_pins) ----------------------------------------------

def _key_word(key=KEY):
    return "0x" + abi.encode(G.POSM_POOL_AND_POSITION_INFO["outputs"], [
        (key["currency0"], key["currency1"], key["fee"], key["tickSpacing"], key["hooks"]),
        123]).hex()


def _rpc(owner=HOLDER, key=KEY):
    def rpc(method, params):
        assert method == "eth_call"
        call = params[0]
        sel = call["data"][:10]
        if sel == abi.selector("ownerOf(uint256)"):
            assert call["to"].lower() == NPM
            return "0x" + abi.encode([{"type": "address"}], [owner]).hex()
        if sel == abi.selector("getPoolAndPositionInfo(uint256)"):
            assert call["to"].lower() == NPM
            return _key_word(key)
        raise AssertionError(f"unexpected call {sel}")
    return rpc


@pytest.fixture
def pins(monkeypatch):
    monkeypatch.setattr(dex_registry, "verify_pins", lambda *a: None)


def test_currencies_must_be_the_positions_pool_key(pins):
    G.verify_pins(_rpc(), _intent(), _tx())
    other = dict(KEY, currency1=OTHER)
    with pytest.raises(ValueError, match="TAKE_PAIR takes"):
        G.verify_pins(_rpc(key=other), _intent(), _tx())


def test_an_unpinned_hook_on_the_position_refuses(pins):
    with pytest.raises(ValueError, match="no pinned code hash"):
        G.verify_pins(_rpc(key=dict(KEY, hooks="0x" + "1" * 40)), _intent(), _tx())


def test_an_empty_position_key_read_refuses(pins):
    with pytest.raises(ValueError, match="returned nothing"):
        G.verify_pins(lambda m, p: "0x", _intent(), _tx())


# -- the events ----------------------------------------------------------------

def _log(salt=POS, delta=0, sender=NPM):
    data = (b"\x00" * 64 + delta.to_bytes(32, "big", signed=True) + salt.to_bytes(32, "big"))
    return {"address": PM, "topics": [G.TOPIC_V4_MODIFY_LIQUIDITY, "0x" + "00" * 32,
                                      "0x" + sender[2:].lower().rjust(64, "0")],
            "data": "0x" + data.hex()}


def test_collect_events_are_ours_and_change_nothing():
    G.assert_position_events(_intent(), [], POS)
    G.assert_position_events(_intent(), [_log()], POS)
    for logs, match in (([_log(delta=-5)], "may not change"), ([_log(delta=5)], "may not change"),
                        ([_log(salt=POS + 1)], "different position"),
                        ([_log(), _log()], "at most one")):
        with pytest.raises(ValueError, match=match):
            G.assert_position_events(_intent(), logs, POS)


# -- through the real authorizer ------------------------------------------------

def _deltas(**kw):
    base = dict(ok=True, token_deltas={TKN: 5 * 10 ** 18}, native_delta=3 * 10 ** 15,
                gas_used=210_000, logs=(_log(),))
    base.update(kw)
    return Deltas(**base)


def _authorize(intent, tx, *, holder=HOLDER, owner=HOLDER, deltas=None, monkeypatch=None):
    return tx_guard.authorize(
        intent, tx, holder=holder, gate=PolicyGate(max_per_tx_usd=100, daily_cap_usd=1000),
        simulate_fn=lambda **kw: deltas or _deltas(), liquidity_rpc=_rpc(owner=owner),
        account_rpc=lambda *a: None, price_fn=lambda *a: 1.0, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, rpc_is_pinned_fn=lambda c: True)


@pytest.fixture
def guard_env(monkeypatch, pins):
    monkeypatch.setattr(tx_guard, "_decimals_for", lambda *a: 18)
    monkeypatch.setattr(tx_guard, "_via_account_preflight", lambda *a, **kw: None)


def test_the_wallets_own_collect_passes_the_authorizer(guard_env):
    d = _authorize(_intent(), _tx())
    assert d.allowed, d.reason


def test_the_collect_through_an_account_passes_the_authorizer(guard_env):
    inner = _tx(to=ACCT)
    outer = {"to": ACCT, "chainId": 4663, "value": 0, "maxFeePerGas": 10 ** 9,
             "data": erc6551.encode_execute(NPM, 0, inner["data"], 0)}
    intent = dataclasses.replace(_intent(), via_account=ACCT, via_account_state=3)
    d = _authorize(intent, outer, owner=ACCT)
    assert d.allowed, d.reason
    # the position is the WALLET's, not the account's: the owner read refuses
    d = _authorize(intent, outer, owner=HOLDER)
    assert not d.allowed and "does not own" in d.reason
    # TAKE_PAIR to the wallet while the account is the holder refuses
    outer_w = dict(outer, data=erc6551.encode_execute(NPM, 0, _tx(to=HOLDER)["data"], 0))
    d = _authorize(intent, outer_w, owner=ACCT)
    assert not d.allowed and "not the holder" in d.reason


@pytest.mark.parametrize("change,match", [
    (dict(token_deltas={TKN: -1}), "below minimum"),                 # the token LEFT
    (dict(native_delta=-10 ** 15), "below minimum"),                 # native LEFT
    (dict(token_deltas={TKN: 1, OTHER: -1}), "UNDECLARED"),          # another token moved
    (dict(holder_nft_out=((NPM, "erc721", OTHER, POS, 1),)), "must not transfer any NFT"),
    (dict(holder_nft_in=((NPM, "erc721", ZERO, POS + 1, 1),)), "must not transfer any NFT"),
    (dict(logs=(_log(delta=-1),)), "may not change"),
])
def test_the_measured_result_is_inflows_only(guard_env, change, match):
    d = _authorize(_intent(), _tx(), deltas=_deltas(**change))
    assert not d.allowed and match in d.reason, d.reason


def test_a_v3_liquidity_call_through_an_account_still_refuses(guard_env):
    npm3 = dex_registry.row_for("base", "v3").position_manager
    intent = tx_guard.TxIntent(
        chain="base", token=None, to=npm3, amount_raw=0, max_spend_usd=5.0,
        is_liquidity_op=True, lp_inflows=((OTHER, 1), (TKN, 1)), lp_position=(npm3, POS),
        lp_position_effect="hold", via_account=ACCT, via_account_state=3)
    outer = {"to": ACCT, "chainId": 8453, "value": 0,
             "data": erc6551.encode_execute(npm3, 0, "0x00", 0)}
    d = _authorize(intent, outer)
    assert not d.allowed and "collect the fees of a v4 position" in d.reason


# -- 117 W7: the withdrawal is the same two actions with a positive decrease ------

W_LIQ = 10 ** 18


def _withdraw_tx(liquidity=W_LIQ, min0=5, min1=7, to=HOLDER):
    return _tx(unlock=_unlock([0x01, 0x11], [_decrease(liquidity=liquidity, min0=min0, min1=min1),
                                             _take(to=to)]))


def _withdraw_intent(**kw):
    pm = dex_registry.row_for("robinhood", "v4").pool_manager
    base = dict(lp_inflows=((None, 5), (TKN, 7)),
                expected_events=((pm, G.TOPIC_V4_MODIFY_LIQUIDITY),))
    base.update(kw)
    return _intent(**base)


def test_a_withdrawal_with_minimums_passes():
    G.structural(_withdraw_intent(), _withdraw_tx(), holder=HOLDER)
    assert G.decode_v4_collect(_withdraw_tx())["liquidity"] == W_LIQ


def test_a_withdrawal_needs_an_on_chain_minimum_a_declared_receipt_and_the_event():
    with pytest.raises(ValueError, match="positive on-chain minimum"):
        G.structural(_withdraw_intent(), _withdraw_tx(min0=0, min1=0), holder=HOLDER)
    with pytest.raises(ValueError, match="positive minimum receipt"):
        G.structural(_withdraw_intent(lp_inflows=((None, 0), (TKN, 0))), _withdraw_tx(), holder=HOLDER)
    with pytest.raises(ValueError, match="ModifyLiquidity event"):
        G.structural(_withdraw_intent(expected_events=()), _withdraw_tx(), holder=HOLDER)


def test_a_withdrawal_pays_the_holder_only_and_never_rides_an_account():
    with pytest.raises(ValueError, match="not the holder"):
        G.structural(_withdraw_intent(), _withdraw_tx(to=OTHER), holder=HOLDER)
    import dataclasses
    through = dataclasses.replace(_withdraw_intent(), via_account=ACCT)
    with pytest.raises(ValueError, match="only to collect fees"):
        G.structural(through, _withdraw_tx(to=ACCT), holder=ACCT)


def _modify_log(delta, salt=POS):
    row = dex_registry.row_for("robinhood", "v4")
    data = b"\x00" * 64 + delta.to_bytes(32, "big", signed=True) + salt.to_bytes(32, "big")
    return {"address": row.pool_manager, "data": "0x" + data.hex(),
            "topics": [G.TOPIC_V4_MODIFY_LIQUIDITY, "0x" + "7" * 64,
                       "0x" + row.position_manager[2:].lower().rjust(64, "0")]}


def test_the_withdrawal_event_removes_exactly_the_signed_liquidity():
    intent, tx = _withdraw_intent(), _withdraw_tx()
    G.assert_position_events(intent, [_modify_log(-W_LIQ)], POS, tx)
    for logs in ([], [_modify_log(-W_LIQ + 1)], [_modify_log(W_LIQ)],
                 [_modify_log(-W_LIQ), _modify_log(-W_LIQ)]):
        with pytest.raises(ValueError, match="exactly one"):
            G.assert_position_events(intent, logs, POS, tx)
    # a collect is still a change of 0, with or without the signed bytes
    with pytest.raises(ValueError, match="may not change"):
        G.assert_position_events(_intent(), [_modify_log(-1)], POS, _tx())
