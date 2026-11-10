"""A resource call re-checked by polyrob-signer (no execution context, no order store).

The signer refused every resource call ("only for a run that fulfils an order"), so no
contract-resource order could be fulfilled with WALLET_SIGNER=remote. The re-check now
skips only that refusal and asserts the receipt against the recipient the agent's guard
bound to the order (``TxIntent.resource_recipient``)."""
from types import SimpleNamespace

from core.wallet import tx_guard
from core.wallet.tx_guard_steps.context import GuardContext
from core.wallet.tx_guard_steps.turn import step_2_turn_origin

CONTRACT = "0x" + "11" * 20
RECIPIENT = "0x" + "ab" * 20
HOLDER = "0x" + "22" * 20

_NONE = ("tool_self", "simulate_fn", "price_fn", "fallback_price_fn", "rpc_is_pinned_fn",
         "halted_fn", "entry_paused_fn", "forged_fn", "autonomous_ok_fn", "liquidity_rpc",
         "account_rpc")


def _ctx(intent, execution_context=None):
    return GuardContext(intent=intent, tx={"to": CONTRACT, "data": "0x12345678", "value": 0,
                                           "nonce": 1},
                        holder=HOLDER, gate=None, execution_context=execution_context,
                        **{k: None for k in _NONE})


def _intent(**kw):
    return tx_guard.TxIntent(chain="base", token=None, to=CONTRACT, amount_raw=0,
                             max_spend_usd=1.0, is_resource_call=True, **kw)


def test_signer_recheck_admits_a_resource_call_with_its_recipient():
    c = _ctx(_intent(resource_recipient=RECIPIENT))
    assert step_2_turn_origin(c) is None
    assert c.offer_recipient == RECIPIENT


def test_a_run_that_is_not_an_order_still_refuses_a_resource_call():
    ctx = SimpleNamespace(user_id="owner", session_id="s", metadata={}, is_sub_agent=False,
                          role="orchestrator")
    d = step_2_turn_origin(_ctx(_intent(), ctx))
    assert d is not None and not d.allowed and "fulfils an order" in d.reason


def test_an_order_run_refuses_a_recipient_that_is_not_the_orders(monkeypatch):
    from core.money import hooks
    monkeypatch.setattr(hooks, "offer_recipient", lambda order_id: RECIPIENT)
    ctx = SimpleNamespace(user_id="owner", session_id="s", metadata={"mandate": "ord_1"},
                          is_sub_agent=False, role="orchestrator")
    d = step_2_turn_origin(_ctx(_intent(resource_recipient="0x" + "99" * 20), ctx))
    assert d is not None and not d.allowed and "not the order's recipient" in d.reason


def test_resource_recipient_crosses_the_signer_wire():
    from core.signer import protocol
    wire = protocol.intent_to_wire(_intent(resource_recipient=RECIPIENT))
    assert protocol.intent_from_wire(wire).resource_recipient == RECIPIENT
