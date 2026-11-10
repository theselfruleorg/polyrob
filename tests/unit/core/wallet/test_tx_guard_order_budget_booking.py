"""099: an order run's spend is booked against the order's budget by the GUARD, after
its final allow (``tx_guard_steps.run`` → ``turn.reserve_offer_budget``). Step 2 only
checks the budget, so without the booking an order could spend past it.

* an allowed send books its declared maximum once;
* the same idempotency key (a dry run, then the real send) books nothing more;
* a send a LATER step refuses (the spend ledger's per-tx cap) books nothing."""
from types import SimpleNamespace

import pytest

from core.commerce import orders as ords
from core.commerce import store as st
from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST

RECIPIENT = "0x" + "ab" * 20
HOLDER = "0x" + "22" * 20
CONTRACT = "0x" + "11" * 20


class _Journal:
    def unresolved(self):
        return []

    def mark_booked(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _wiring(monkeypatch):
    from core.money import hooks, offer_gate
    offer_gate.register()
    monkeypatch.setattr(hooks, "submission_journal", lambda: _Journal())
    import importlib
    regime = importlib.import_module("core.config_policy.money_regime")
    monkeypatch.setattr(regime, "unattended_money_allowed", lambda: True)
    monkeypatch.setattr("core.instance.is_owner_uid",
                        lambda uid, allow_local=False: uid == "owner")
    yield
    hooks._PROVIDERS.pop("offer_gate", None)
    hooks._PROVIDERS.pop("offer_recipient", None)


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


def _send(order_id, *, key="k1", per_tx=100.0):
    amount = 10 ** 14                                     # 0.0001 ETH at $2000 = $0.20
    intent = tx_guard.TxIntent(chain="base", token=None, to=RECIPIENT, amount_raw=amount,
                               max_spend_usd=0.2, idempotency_key=key)
    ctx = SimpleNamespace(user_id="owner", session_id="s", metadata={"mandate": order_id},
                          is_sub_agent=False, role="orchestrator")
    return tx_guard.authorize(
        intent, {"to": RECIPIENT, "data": "0x", "value": amount, "chainId": 8453},
        holder=HOLDER, gate=PolicyGate(max_per_tx_usd=per_tx, daily_cap_usd=1000.0),
        execution_context=ctx,
        simulate_fn=lambda **_: Deltas(ok=True, native_delta=-amount, token_deltas={}),
        price_fn=lambda c, a: 2000.0, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda c, t: True, autonomous_ok_fn=lambda c, t: True)


def test_an_allowed_order_send_books_the_budget_once_per_key(order):
    first = _send(order.id)
    assert first.allowed, first.reason
    assert ords.get(order.id).spent_usd == pytest.approx(0.2)
    again = _send(order.id)                               # the real send of a dry run
    assert again.allowed, again.reason
    assert ords.get(order.id).spent_usd == pytest.approx(0.2)


def test_a_send_a_later_step_refuses_books_nothing(order):
    refused = _send(order.id, per_tx=0.01)
    assert not refused.allowed and "per-transaction cap" in refused.reason
    assert ords.get(order.id).spent_usd == 0.0
