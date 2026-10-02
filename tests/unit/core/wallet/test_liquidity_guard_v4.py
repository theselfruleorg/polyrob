"""048 phase 3 / core handoff W6: the guard judges a v4 add from the SIGNED
calldata, and recognises exactly one Permit2 grant."""
import time

import pytest

from core.wallet import dex_registry, liquidity_guard as G, tx_guard
from tools.defi import lp_reads, lp_v4 as V

PNL = "0xbba60ab93fc409b1a34371cbf6c3173795ed2c7e"
HOLDER = "0x" + "ab" * 20
ROW = dex_registry.row_for("robinhood", "v4")
NPM, PM, P2 = ROW.position_manager, ROW.pool_manager, ROW.permit2
KEY = lp_reads.pons_pool_key({"token": PNL, "pairToken": "0x" + "0" * 40,
                              "poolFee": 0, "tickSpacing": 200})
MAX0, MAX1 = 10 ** 16, 7 * 10 ** 23


def _tx(key=KEY, owner=HOLDER, a0=MAX0, a1=MAX1, value=MAX0, data=None):
    unlock = V.encode_mint_unlock(key, -887200, 887200, 10 ** 20, a0, a1, owner)
    return {"to": NPM, "chainId": 4663, "value": value,
            "data": data or V.encode_modify_liquidities(unlock, int(time.time()) + 120)}


def _intent(**kw):
    base = dict(chain="robinhood", token=None, to=NPM, amount_raw=0, max_spend_usd=100.0,
                is_liquidity_op=True, lp_outflows=((None, MAX0), (PNL, MAX1)),
                watch_spenders=(P2,), lp_position=(NPM, None), lp_position_effect="mint",
                expected_events=((PM, V.TOPIC_MODIFY_LIQUIDITY),))
    base.update(kw)
    return tx_guard.TxIntent(**base)


def test_protocol_is_read_from_the_pinned_target():
    assert G.protocol_of(_intent()) == "v4"
    assert G.protocol_of(_intent(to="0x73991a25c818bf1f1128deaab1492d45638de0d3")) == "v3"


def test_the_one_v4_shape_passes():
    G.structural(_intent(), _tx())
    assert G.TOPIC_V4_MODIFY_LIQUIDITY == V.TOPIC_MODIFY_LIQUIDITY


def test_calldata_may_not_pay_more_than_declared():
    with pytest.raises(ValueError, match="may pay"):
        G.structural(_intent(lp_outflows=((None, MAX0), (PNL, MAX1 - 1))), _tx())


def test_an_unpinned_hook_refuses():
    key = dict(KEY, hooks="0x" + "1" * 40)
    with pytest.raises(ValueError, match="no pinned code hash"):
        G.structural(_intent(), _tx(key=key))


def test_value_must_equal_the_native_leg():
    with pytest.raises(ValueError, match="tx.value"):
        G.structural(_intent(), _tx(value=MAX0 - 1))


def test_v4_refuses_everything_but_mint():
    with pytest.raises(ValueError, match="MINT only"):
        G.structural(_intent(lp_position=(NPM, 5), lp_position_effect="hold"), _tx())


def test_another_action_sequence_refuses():
    from core.wallet import abi
    unlock = abi.encode([{"type": "bytes"}, {"type": "bytes[]"}],
                        [bytes([0x02, 0x0D]), [b"", b""]])
    with pytest.raises(ValueError, match="exactly MINT_POSITION"):
        G.structural(_intent(), _tx(data=V.encode_modify_liquidities(unlock, 1)))


def test_the_modify_liquidity_event_must_be_required():
    with pytest.raises(ValueError, match="ModifyLiquidity"):
        G.structural(_intent(expected_events=()), _tx())


def _log(salt, delta=10 ** 20, sender=NPM):
    data = (b"\x00" * 64 + delta.to_bytes(32, "big", signed=True) + salt.to_bytes(32, "big"))
    return {"address": PM, "topics": [V.TOPIC_MODIFY_LIQUIDITY, lp_reads.pool_id(KEY),
                                      "0x" + sender[2:].lower().rjust(64, "0")],
            "data": "0x" + data.hex()}


def test_position_event_must_be_ours_and_adding():
    G.assert_position_events(_intent(), [_log(7)], 7)
    with pytest.raises(ValueError, match="different position"):
        G.assert_position_events(_intent(), [_log(8)], 7)
    with pytest.raises(ValueError, match="withdraw"):
        G.assert_position_events(_intent(), [_log(7, delta=-1)], 7)
    with pytest.raises(ValueError, match="exactly one"):
        G.assert_position_events(_intent(), [], 7)
    # a hook's own modification (sender != PositionManager) is not the position's
    G.assert_position_events(_intent(), [_log(7), _log(9, sender="0x" + "2" * 40)], 7)


# -- the one Permit2 grant -------------------------------------------------

def _grant(spender=NPM, amount=MAX1, exp=None, declared=MAX1, to=P2):
    exp = int(time.time()) + 900 if exp is None else exp
    tx = {"to": to, "data": V.encode_permit2_approve(PNL, spender, amount, exp)}
    intent = tx_guard.TxIntent(chain="robinhood", token=PNL, to=spender, amount_raw=0,
                               max_spend_usd=10.0, is_allowance_op=True,
                               expected_allowance_grants=((PNL, spender, declared),))
    return intent, tx


def test_the_exact_expiring_grant_to_the_pinned_posm_is_recognised():
    intent, tx = _grant()
    assert G.permit2_grant_declared(intent, tx, P2, PNL, NPM, MAX1)


@pytest.mark.parametrize("kw,emitted", [
    (dict(spender="0x" + "3" * 40), None),                     # another spender
    (dict(exp_in=G.PERMIT2_GRANT_MAX_TTL_S + 60), None),       # lives too long
    (dict(exp_in=-1), None),                                   # already expired
    (dict(declared=MAX1 - 1), None),                           # more than declared
    (dict(to="0x" + "4" * 40), None),                          # not the pinned Permit2
    (dict(), MAX1 + 1),                                        # emitted != signed
])
def test_every_other_grant_refuses(kw, emitted):
    kw = dict(kw)
    if "exp_in" in kw:                 # relative to NOW, not to collection time
        kw["exp"] = int(time.time()) + kw.pop("exp_in")
    intent, tx = _grant(**kw)
    spender = kw.get("spender", NPM)
    assert not G.permit2_grant_declared(intent, tx, P2, PNL, spender, emitted or MAX1)


def test_a_grant_through_a_token_bound_account_is_not_recognised():
    intent, tx = _grant()
    import dataclasses
    intent = dataclasses.replace(intent, via_account="0x" + "5" * 40)
    assert not G.permit2_grant_declared(intent, tx, P2, PNL, NPM, MAX1)


# -- the LP program caps (090 R4) -------------------------------------------

class _Gate:
    def __init__(self, rows):
        self.audit_log = rows

    def _sync_shared_ledger(self):
        return True


def test_caps_default_refuses_until_the_owner_sets_a_program_cap(monkeypatch):
    from tools.defi.lp_v4_verbs import caps_refusal
    monkeypatch.delenv("LP_ETH_CAP", raising=False)
    assert "LP_ETH_CAP is 0" in caps_refusal(_Gate([]), 10 ** 17, 10 ** 19)


def test_caps_program_daily_and_floor(monkeypatch):
    from tools.defi.lp_v4_verbs import caps_refusal
    monkeypatch.setenv("LP_ETH_CAP", "5")
    monkeypatch.setenv("LP_ETH_DAILY_CAP", "2")
    monkeypatch.setenv("LP_ETH_FLOOR", "0.5")
    now = 10 ** 9
    old = {"action": "lp_add", "native_raw": 4 * 10 ** 18, "ts": now - 2 * 86400}
    today = {"action": "lp_add", "native_raw": 18 * 10 ** 17, "ts": now - 60}
    assert caps_refusal(_Gate([old]), 10 ** 18, 10 ** 19, now) is None
    assert "LP_ETH_CAP" in caps_refusal(_Gate([old]), 11 * 10 ** 17, 10 ** 19, now)
    assert "DAILY" in caps_refusal(_Gate([today]), 3 * 10 ** 17, 10 ** 19, now)
    assert "FLOOR" in caps_refusal(_Gate([]), 10 ** 18, 14 * 10 ** 17, now)
    assert caps_refusal(_Gate([]), 0, 0, now) is None


def test_an_unreadable_ledger_refuses(monkeypatch):
    from tools.defi.lp_v4_verbs import caps_refusal
    monkeypatch.setenv("LP_ETH_CAP", "5")
    gate = _Gate([])
    gate._sync_shared_ledger = lambda: False
    assert "unreadable" in caps_refusal(gate, 10 ** 17, 10 ** 19)
