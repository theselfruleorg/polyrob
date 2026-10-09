"""The signed transaction must pay the DECLARED destination.

The guard compared amounts only for native and token sends, so an intent that says
"send to Alice" could carry a transaction that pays someone else (and the owner's
approval card and the ledger name Alice). A revoke-shaped intent (priced $0, treated
as an exit) could carry any zero-movement call."""
from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ALICE = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
MALLORY = "0x3333333333333333333333333333333333333333"
HOLDER = "0x2222222222222222222222222222222222222222"
SPENDER = "0x1111111111111111111111111111111111111111"


def _word(addr):
    return "0" * 24 + addr.lower()[2:]


def _transfer(to, amount=250_000):
    return "0xa9059cbb" + _word(to) + f"{amount:064x}"


def _run(intent, tx, deltas, *, native_price=3000.0):
    return tx_guard.authorize(
        intent, {"value": 0, "chainId": 8453, **tx}, holder=HOLDER,
        gate=PolicyGate(max_per_tx_usd=1000.0, daily_cap_usd=10_000.0),
        execution_context=None, simulate_fn=lambda **_: deltas,
        price_fn=lambda c, a: 1.0 if a and a.lower() == USDC.lower() else native_price,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda c, t: False)


def _send(to=ALICE):
    return tx_guard.TxIntent(chain="base", token=USDC, to=to, amount_raw=250_000,
                             max_spend_usd=5.0, idempotency_key="k")


def _deltas(transfers=()):
    return Deltas(ok=True, native_delta=0, token_deltas={USDC: -250_000},
                  holder_transfers=tuple(transfers))


def test_a_token_send_whose_calldata_pays_the_declared_recipient_passes():
    d = _run(_send(), {"to": USDC, "data": _transfer(ALICE)}, _deltas([(USDC, ALICE, 250_000)]))
    assert d.allowed, d.reason


def test_a_token_send_whose_calldata_pays_someone_else_refuses():
    d = _run(_send(), {"to": USDC, "data": _transfer(MALLORY)}, _deltas())
    assert not d.allowed and "does not pay the declared destination" in d.reason


def test_a_measured_transfer_to_someone_else_refuses_whatever_the_calldata_says():
    d = _run(_send(), {"to": USDC, "data": _transfer(ALICE)}, _deltas([(USDC, MALLORY, 250_000)]))
    assert not d.allowed and MALLORY in d.reason


def test_a_native_send_to_another_address_than_declared_refuses():
    intent = tx_guard.TxIntent(chain="base", token=None, to=ALICE, amount_raw=10 ** 15,
                               max_spend_usd=50.0, idempotency_key="n")
    deltas = Deltas(ok=True, native_delta=-(10 ** 15), token_deltas={})
    refused = _run(intent, {"to": MALLORY, "data": "0x", "value": 10 ** 15}, deltas)
    assert not refused.allowed and "not to the declared destination" in refused.reason
    assert _run(intent, {"to": ALICE, "data": "0x", "value": 10 ** 15}, deltas).allowed


def _revoke():
    return tx_guard.TxIntent(chain="base", token=USDC, to=SPENDER, amount_raw=0,
                             max_spend_usd=0.01, is_allowance_op=True, idempotency_key="r")


def test_a_revoke_must_be_exactly_approve_spender_zero_on_the_declared_token():
    deltas = Deltas(ok=True, native_delta=0, token_deltas={USDC: 0},
                    allowance_deltas={(USDC, SPENDER): 0})
    good = {"to": USDC, "data": "0x095ea7b3" + _word(SPENDER) + "0" * 64}
    assert _run(_revoke(), good, deltas).allowed
    # transferOwnership(mallory) on another contract, declared as a revoke
    forged = {"to": "0x4444444444444444444444444444444444444444",
              "data": "0xf2fde38b" + _word(MALLORY)}
    d = _run(_revoke(), forged, deltas)
    assert not d.allowed and "a revoke must be exactly approve" in d.reason
    # approve(spender, nonzero) declared as a revoke
    grant = {"to": USDC, "data": "0x095ea7b3" + _word(SPENDER) + f"{1:064x}"}
    assert not _run(_revoke(), grant, deltas).allowed
