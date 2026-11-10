"""099 W2: the money kernel's offer step and the resource-call receipt."""
from types import SimpleNamespace

import pytest

from core.commerce import orders as ords
from core.commerce import store as st
from core.money.authorize import SpendIntent, authorize_spend
from core.wallet import resource_call
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST

RECIPIENT = "0x" + "ab" * 20
MINT = st.selector("mint(address,string)")
CONTRACT = "0x" + "11" * 20
OTHER = "0x" + "99" * 20


@pytest.fixture(autouse=True)
def _gate():
    from core.money import hooks, offer_gate
    offer_gate.register()
    yield
    hooks._PROVIDERS.pop("offer_gate", None)


@pytest.fixture
def order(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: str(tmp_path / "commerce.db"))
    o = st.save_draft("owner", MANIFEST)
    st.add_resource(o.id, "owner", "contract", {"runtime_hash": "0x" + "ab" * 32,
                                                "functions": ["mint(address,string)"]},
                    chain="base", address=CONTRACT)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.card_hash(o))
    order = ords.create(offer_id=o.id, offer_version=1, user_id="owner", channel="a2a",
                        buyer="b", inputs={"delivery_address": RECIPIENT, "brief": "x"},
                        amount_usd="5.00", spend_budget=0.5)
    ords.attach_invoice(order.id, "inv_9", "5.00")
    ords.mark_paid("inv_9")
    tok = ords.claim_fulfillment(order.id)
    ords.set_goal(order.id, tok, "g_order")
    from core.autonomy_marker import mark_autonomous
    mark_autonomous("s", "g_order")
    return order


def _ctx(order_id, user="owner"):
    return SimpleNamespace(user_id=user, session_id="s", metadata={"mandate": order_id},
                           is_sub_agent=False, role="orchestrator")


def _verdict(order_id, scope, usd=0.2, key="k1", user="owner"):
    return authorize_spend(
        SpendIntent.turn_origin(usd=usd, offer_scope=scope, idempotency_key=key),
        _ctx(order_id, user), forged_fn=lambda c, t: True, autonomous_ok_fn=lambda c, t: True)


def test_no_registered_gate_fails_closed(order):
    from core.money import hooks
    hooks._PROVIDERS.pop("offer_gate", None)
    v = _verdict(order.id, {"chain": "base", "destinations": []})
    assert v.refused and "no offer gate is registered" in v.reason


def test_order_run_without_guard_facts_is_refused(order):
    v = _verdict(order.id, None)
    assert v.refused and v.step == "offer" and "guarded on-chain verbs" in v.reason


def test_a_resource_call_inside_the_envelope_is_admitted_once_per_key(order):
    scope = {"chain": "base", "destinations": [], "is_resource_call": True, "contract": CONTRACT,
             "selector": MINT}
    assert not _verdict(order.id, scope).refused
    assert not _verdict(order.id, scope).refused            # dry run + real: same key
    assert ords.get(order.id).spent_usd == pytest.approx(0.2)
    assert _verdict(order.id, scope, usd=0.4, key="k2").refused   # 0.2 + 0.4 > 0.5


def test_paying_anyone_but_the_recipient_is_refused(order):
    ok = {"chain": "base", "destinations": [RECIPIENT], "is_resource_call": False}
    assert not _verdict(order.id, ok).refused
    bad = dict(ok, destinations=[OTHER])
    v = _verdict(order.id, bad, key="k3")
    assert v.refused and "neither this order's recipient" in v.reason


def test_a_resource_call_to_a_non_resource_is_refused(order):
    scope = {"chain": "base", "destinations": [], "is_resource_call": True, "contract": OTHER}
    v = _verdict(order.id, scope)
    assert v.refused and "contract resource" in v.reason


def test_a_resource_added_after_go_live_has_no_authority(order):
    late = "0x" + "22" * 20
    st.add_resource(order.offer_id, "owner", "contract", {"runtime_hash": "0x" + "cd" * 32,
                                                         "functions": ["mint(address,string)"]},
                    chain="base", address=late)
    scope = {"chain": "base", "destinations": [], "is_resource_call": True, "contract": late}
    assert _verdict(order.id, scope, key="late").refused


def test_only_confirmed_functions_and_only_the_current_goal(order):
    scope = {"chain": "base", "destinations": [], "is_resource_call": True,
             "contract": CONTRACT, "selector": st.selector("transferOwnership(address)")}
    v = _verdict(order.id, scope, key="own")
    assert v.refused and "not one the owner confirmed" in v.reason
    from core.autonomy_marker import mark_autonomous
    mark_autonomous("s", "g_old_attempt")
    ok = dict(scope, selector=MINT)
    v = _verdict(order.id, ok, key="old")
    assert v.refused and "current fulfillment goal" in v.reason
    mark_autonomous("s", "g_order")


def test_a_value_call_to_a_pinned_contract_still_needs_a_pinned_function(order):
    """Review C2: a call with value (or a token spend) is not resource-call-shaped, but
    it addresses the offer's contract — the function pin must still bind it."""
    scope = {"chain": "base", "destinations": [CONTRACT], "is_resource_call": False,
             "contract": CONTRACT, "selector": st.selector("transferOwnership(address)")}
    v = _verdict(order.id, scope, key="c2a")
    assert v.refused and "not one the owner confirmed" in v.reason
    assert not _verdict(order.id, dict(scope, selector=MINT), key="c2b").refused
    # an ERC-20 transfer to the recipient (tx.to = a token, not a resource) is unchanged
    pay = {"chain": "base", "destinations": [RECIPIENT], "is_resource_call": False,
           "contract": OTHER, "selector": st.selector("transfer(address,uint256)")}
    assert not _verdict(order.id, pay, usd=0.1, key="c2c").refused


def test_a_delivered_order_cannot_spend_and_tenants_are_separate(order):
    scope = {"chain": "base", "destinations": [], "is_resource_call": True, "contract": CONTRACT,
             "selector": MINT}
    assert _verdict(order.id, scope, user="mallory").refused
    ords.deliver(order.id, {"tx_hash": "0x" + "ee" * 32})
    v = _verdict(order.id, scope, key="k9")
    assert v.refused and "one order, one fulfillment" in v.reason


def test_autonomous_admission_needs_no_trading_flag_for_an_order_run(monkeypatch):
    import importlib
    from core.wallet import tx_guard
    regime = importlib.import_module("core.config_policy.money_regime")
    monkeypatch.setattr(tx_guard, "autonomous_turn_trading_enabled", lambda: False)
    monkeypatch.setattr(regime, "unattended_money_allowed", lambda: True)
    ok = lambda c, t: True  # noqa: E731
    assert tx_guard._autonomous_turn_allowed(_ctx("ord_abc"), None, ok, offer_scoped=True)
    assert not tx_guard._autonomous_turn_allowed(_ctx("m_trading"), None, ok)
    # review H1: an order run is admitted ONLY through step 2 with the offer facts —
    # never by another gate (the Solana path), even with the trading flag on.
    assert not tx_guard._autonomous_turn_allowed(_ctx("ord_abc"), None, ok)
    monkeypatch.setattr(tx_guard, "autonomous_turn_trading_enabled", lambda: True)
    assert not tx_guard._autonomous_turn_allowed(_ctx("ord_abc"), None, ok)
    monkeypatch.setattr(regime, "unattended_money_allowed", lambda: False)
    assert not tx_guard._autonomous_turn_allowed(_ctx("ord_abc"), None, ok, offer_scoped=True)


def test_a_spend_with_no_destination_is_refused(order):
    v = _verdict(order.id, {"chain": "base", "destinations": [None], "is_resource_call": False})
    assert v.refused and "names no destination" in v.reason


# -- the guard books an order's budget only for an ALLOWED transaction ------------------

_SCOPE = {"chain": "base", "destinations": [], "is_resource_call": True, "contract": CONTRACT,
          "selector": MINT}


def _dry(order_id, usd, key):
    return authorize_spend(
        SpendIntent.turn_origin(usd=usd, offer_scope=_SCOPE, idempotency_key=key,
                                dry_run=True),
        _ctx(order_id), forged_fn=lambda c, t: True, autonomous_ok_fn=lambda c, t: True)


def _guard_ctx(order_id, key):
    from core.wallet import tx_guard
    return SimpleNamespace(
        offer_scope=dict(_SCOPE), execution_context=_ctx(order_id), forged_fn=lambda c, t: True,
        tool_self=None, intent=tx_guard.TxIntent(chain="base", token=None, to=CONTRACT,
                                                 amount_raw=0, max_spend_usd=0.4,
                                                 idempotency_key=key))


def test_the_guards_check_books_nothing(order):
    assert not _dry(order.id, 0.4, "k1").refused
    assert ords.get(order.id).spent_usd == 0


def test_the_booking_after_the_allow_is_once_per_key_and_decides_the_budget(order):
    from core.wallet.tx_guard_steps.turn import reserve_offer_budget
    assert reserve_offer_budget(_guard_ctx(order.id, "k1")) is None
    assert reserve_offer_budget(_guard_ctx(order.id, "k1")) is None   # dry run, then the send
    assert ords.get(order.id).spent_usd == pytest.approx(0.4)
    # the real send of a booked dry run passes the check although 0.4 + 0.4 > 0.5
    assert not _dry(order.id, 0.4, "k1").refused
    d = reserve_offer_budget(_guard_ctx(order.id, "k2"))
    assert d is not None and not d.allowed and "spend budget" in d.reason


def test_step_2_only_checks_an_orders_budget(order, monkeypatch):
    from core.wallet.tx_guard_steps import turn
    seen = {}
    import core.money.authorize as kernel

    def spy(intent, *a, **k):
        seen["dry_run"] = intent.dry_run
        return kernel.Verdict("refuse", "stop here", step="turn")
    monkeypatch.setattr(kernel, "authorize_spend", spy)
    from core.wallet import tx_guard
    from core.wallet.tx_guard_steps.context import GuardContext
    none = {k: None for k in ("tool_self", "simulate_fn", "price_fn", "fallback_price_fn",
                              "rpc_is_pinned_fn", "halted_fn", "entry_paused_fn", "forged_fn",
                              "autonomous_ok_fn", "liquidity_rpc", "account_rpc")}
    c = GuardContext(intent=tx_guard.TxIntent(chain="base", token=None, to=CONTRACT,
                                              amount_raw=0, max_spend_usd=0.4,
                                              is_resource_call=True,
                                              resource_recipient=RECIPIENT),
                     tx={"to": CONTRACT, "data": MINT + "00" * 32, "value": 0, "nonce": 1},
                     holder="0x" + "22" * 20, gate=None, execution_context=_ctx(order.id),
                     **none)
    turn.step_2_turn_origin(c)
    assert seen["dry_run"] is True and c.offer_scope["contract"] == CONTRACT


# -- the receipt -----------------------------------------------------------------------

def _log(topics, address=CONTRACT):
    return {"address": address, "topics": topics}


def _deltas(**kw):
    base = dict(native_delta=0, holder_transfers=(), holder_approvals=(), holder_nft_out=(),
                holder_nft_in=(), holder_operator_grants=(), holder_nft_approvals=(),
                holder_permit2_grants=(), holder_6909_approvals=(), token_deltas={}, logs=())
    base.update(kw)
    return SimpleNamespace(**base)


def _word(a):
    return "0x" + "0" * 24 + a[2:]


MINT_TO_RECIPIENT = _log([resource_call.TOPIC_TRANSFER, _word("0x" + "00" * 20),
                          _word(RECIPIENT), "0x" + "0" * 63 + "1"])


def test_receipt_mint_to_the_recipient_passes():
    intent = SimpleNamespace(to=CONTRACT)
    assert resource_call.assert_simulation(intent, _deltas(logs=(MINT_TO_RECIPIENT,)),
                                           "0xholder", RECIPIENT) is None


@pytest.mark.parametrize("deltas,needle", [
    (_deltas(logs=(_log(["0x8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0",
                         _word(CONTRACT), _word(OTHER)]),)), "OwnershipTransferred"),
    (_deltas(logs=(_log([resource_call.TOPIC_TRANSFER, _word("0x" + "00" * 20),
                         _word(OTHER), "0x1"]),)), "not to the order's recipient"),
    (_deltas(logs=(_log(["0xabc"], address=OTHER),)), "touches only the resource"),
    (_deltas(holder_operator_grants=((CONTRACT, OTHER, True),)), "operator approvals"),
    (_deltas(native_delta=-10 ** 15), "native balance"),
])
def test_receipt_refusals(deltas, needle):
    why = resource_call.assert_simulation(SimpleNamespace(to=CONTRACT), deltas, "0xh", RECIPIENT)
    assert why and needle in why
