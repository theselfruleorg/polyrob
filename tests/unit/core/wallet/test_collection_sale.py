"""tx_guard's CLAIM shape on a pinned SALE: ``claim(treasury)`` and ``settle(own records)``
(proposal 115 W8, item 5; polyrob-desk ``PolyrobSale`` SPEC §6.7).

⚠️ The sale pays the treasury back in two ways only: ``settle`` PUSHES each record's excess to the
record's ``to`` (the treasury, which the mint shape forces), ``claim(who)`` pays ``owed[who]``.
Both send nothing and receive native: the claim shape, with the target a pinned sale, both code
pins checked, the receipt event asserted — and every OTHER call that names a pinned sale refuses.
"""
import pytest

from core.wallet import abi, collection_registry, tx_guard
from core.wallet import collection_sale as S
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tests.collection_pins import code_rpc, pin, profile, sale, sale_address

COLLECTION = "0xc011000000000000000000000000000000000001"
SALE = sale_address(COLLECTION)
HOLDER = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
ACCOUNT = "0x4444444444444444444444444444444444444444"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
OWED = 7 * 10 ** 12
BASE = 33 * 10 ** 15
CLEARING = 66 * 10 ** 15


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    pin(monkeypatch, COLLECTION)


def _w(n):
    return format(int(n), "064x")


def _aw(a):
    return "0" * 24 + a.lower()[2:]


class Sale:
    """A fake RPC over the sale: ``records[i] = (to, qty, paidEach, done)``."""

    def __init__(self, records=None, settled=True, unreadable=()):
        self.records = records if records is not None else {
            1: (HOLDER, 2, 70 * 10 ** 15, False), 4: (HOLDER, 1, 99 * 10 ** 15, False),
            2: (OTHER, 1, 80 * 10 ** 15, False), 3: (HOLDER, 1, 70 * 10 ** 15, True)}
        self.settled = settled
        self.unreadable = set(unreadable)

    def __call__(self, method, params):
        if method != "eth_call":
            return NotImplemented
        to, data = params[0]["to"].lower(), params[0]["data"].lower()
        assert to == SALE, to
        if data == S.SETTLED_SELECTOR:
            return "0x" + _w(1 if self.settled else 0)
        if data.startswith(S.RECORD_SELECTOR):
            i = int(data[10:], 16)
            if i in self.unreadable or i not in self.records:
                raise RuntimeError("execution reverted: BadRecord")
            who, qty, paid, done = self.records[i]
            return "0x" + _aw(who) + _w(i) + _w(qty) + _w(paid) + _w(1 if done else 0)
        raise AssertionError(data)


def _refund(*ids, rpc=None):
    s = rpc or Sale()
    return sum((s.records[i][2] - CLEARING) * s.records[i][1] for i in ids)


def _tx(data=None, to=SALE, value=0):
    return {"to": to, "data": data if data is not None else S.encode_claim(HOLDER), "value": value,
            "chainId": 4663, "nonce": 1, "gas": 200_000, "maxFeePerGas": 10 ** 8}


def _intent(**kw):
    base = dict(chain="robinhood", token=None, to=SALE, amount_raw=0, max_spend_usd=5.0,
                idempotency_key="k", is_claim=True, min_native_inflow_wei=OWED)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _log(topic, *words, address=SALE, topics=()):
    return {"address": address, "topics": [topic, *topics],
            "data": "0x" + "".join(words)}


def _claimed(who=HOLDER, amount=OWED):
    return _log(S.TOPIC_CLAIMED, _aw(who), _w(amount))


def _settled(i, refund, top_up=10 ** 15):
    return _log(S.TOPIC_SETTLED, _w(i), _w(refund), _w(top_up))


def _deltas(native=OWED, logs=None, **kw):
    base = dict(ok=True, native_delta=native, token_deltas={}, allowance_deltas={},
                gas_used=60_000, logs=tuple(logs if logs is not None else [_claimed()]))
    base.update(kw)
    return Deltas(**base)


def _authorize(intent, deltas, *, tx=None, rpc=None):
    return tx_guard.authorize(
        intent, tx if tx is not None else _tx(),
        holder=HOLDER, gate=PolicyGate(max_per_tx_usd=1000.0, daily_cap_usd=5000.0),
        execution_context=None, simulate_fn=lambda **_: deltas,
        price_fn=lambda chain, addr: 4000.0,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False,
        account_rpc=code_rpc(rpc or Sale()))


def _settle(ids=(1, 4), rpc=None, **kw):
    s = rpc or Sale()
    total = _refund(*ids, rpc=s)
    intent = _intent(min_native_inflow_wei=total, **kw)
    deltas = _deltas(native=total, logs=[_settled(i, (s.records[i][2] - CLEARING) * s.records[i][1])
                                         for i in ids])
    return intent, deltas, _tx(S.encode_settle(ids))


# --- the literals --------------------------------------------------------------------------

def test_selectors_and_topics_are_computed_from_their_signatures():
    keccak = pytest.importorskip("eth_utils").keccak
    assert S.CLAIM_SELECTOR == abi.selector(S.CLAIM_SIGNATURE) == "0x1e83409a"
    assert S.SETTLE_SELECTOR == abi.selector(S.SETTLE_SIGNATURE) == "0x9b2db373"
    for lit, sig in ((S.OWED_SELECTOR, "owed(address)"), (S.PHASE_SELECTOR, "phase()"),
                     (S.SETTLED_SELECTOR, "settled()"), (S.CLEARING_SELECTOR, "clearing()"),
                     (S.RECORD_SELECTOR, "record(uint256)")):
        assert lit == abi.selector(sig), sig
    for lit, sig in ((S.TOPIC_SETTLED, "Settled(uint256,uint256,uint256)"),
                     (S.TOPIC_CLAIMED, "Claimed(address,uint256)"),
                     (S.TOPIC_OWED, "Owed(address,uint256)"),
                     (S.TOPIC_BID, "Bid(uint256,address,uint256,uint256,uint256)")):
        assert lit == "0x" + keccak(sig.encode()).hex(), sig


def test_the_codec_round_trips_and_refuses_a_non_canonical_call():
    assert S.decode_call(S.encode_claim(HOLDER)) == ("claim", HOLDER)
    assert S.decode_call(S.encode_settle([1, 4])) == ("settle", [1, 4])
    with pytest.raises(ValueError, match="canonical"):
        S.decode_call(S.encode_claim(HOLDER) + "00")
    with pytest.raises(ValueError, match="at most 20"):
        S.decode_call(S.encode_settle(list(range(21))))
    with pytest.raises(ValueError, match="neither"):
        S.decode_call("0xdeadbeef")


# --- the allows ----------------------------------------------------------------------------

def test_a_claim_of_what_the_sale_owes_the_treasury_is_authorized_at_its_fee():
    d = _authorize(_intent(), _deltas())
    assert d.allowed is True, d.reason
    assert d.amount_usd == pytest.approx(200_000 * 10 ** 8 / 1e18 * 4000, rel=0.5)


def test_a_settle_of_the_treasurys_own_open_records_is_authorized():
    intent, deltas, tx = _settle()
    d = _authorize(intent, deltas, tx=tx)
    assert d.allowed is True, d.reason


def test_a_settle_may_book_an_accounts_failed_top_up():
    intent, deltas, tx = _settle(ids=(1,))
    deltas = _deltas(native=deltas.native_delta, logs=[*deltas.logs, _log(S.TOPIC_OWED, _aw(ACCOUNT), _w(5))])
    assert _authorize(intent, deltas, tx=tx).allowed is True


def test_a_claim_to_any_other_contract_is_not_judged_here():
    assert S.gate(_intent(to=OTHER), _tx(to=OTHER), HOLDER) == (None, None)


def test_an_untrusted_registry_refuses_only_what_it_guards(monkeypatch):
    def boom():
        raise collection_registry.CollectionRegistryError("unknown spec")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    assert S.gate(_intent(), _tx(), HOLDER) == (None, None)


# --- no other call may name a pinned sale -------------------------------------------------

def test_a_zero_value_call_to_the_sale_that_is_not_a_claim_refuses():
    plain = tx_guard.TxIntent(chain="robinhood", token=None, to=SALE, amount_raw=0,
                              max_spend_usd=1.0, idempotency_key="k")
    kind, why = S.gate(plain, _tx(data="0x3ccfd60b"), HOLDER)   # withdraw()
    assert kind is None and "only the guarded" in why


def test_an_approval_naming_the_sale_as_spender_refuses():
    revoke = tx_guard.TxIntent(chain="robinhood", token=USDC, to=SALE, amount_raw=0,
                               max_spend_usd=1.0, idempotency_key="k", is_allowance_op=True)
    tx = _tx(to=USDC, data="0x095ea7b3" + _aw(SALE) + _w(0))
    d = _authorize(revoke, _deltas(native=0, logs=[]), tx=tx)
    assert d.allowed is False and "pinned sale" in d.reason


def test_a_resource_call_to_the_sale_refuses():
    rc = tx_guard.TxIntent(chain="robinhood", token=None, to=SALE, amount_raw=0,
                           max_spend_usd=1.0, idempotency_key="k", is_resource_call=True)
    kind, why = S.gate(rc, _tx(), HOLDER)
    assert kind is None and "only the guarded" in why


def test_a_claim_with_another_selector_refuses():
    d = _authorize(_intent(), _deltas(), tx=_tx(data="0x3ccfd60b"))
    assert d.allowed is False and "not a claim or settle" in d.reason


def test_a_claim_with_value_refuses_as_a_payment():
    d = _authorize(_intent(), _deltas(), tx=_tx(value=1))
    assert d.allowed is False and "paid only by the guarded mint" in d.reason


# --- the claim's structure ----------------------------------------------------------------

def test_a_claim_for_another_address_refuses():
    d = _authorize(_intent(), _deltas(), tx=_tx(S.encode_claim(OTHER)))
    assert d.allowed is False and "not the signing treasury" in d.reason


def test_a_claim_without_a_minimum_refuses():
    kind, why = S.gate(_intent(min_native_inflow_wei=None), _tx(), HOLDER)
    assert kind is None and "min_native_inflow_wei" in why


@pytest.mark.parametrize("extra", [dict(token=USDC), dict(inflow_token=USDC, min_inflow_raw=1),
                                   dict(mint_ids=(1,)), dict(expected_events=(("a", "b"),))])
def test_a_claim_that_declares_anything_else_refuses(extra):
    kind, why = S.gate(_intent(**extra), _tx(), HOLDER)
    assert kind is None and why


@pytest.mark.parametrize("flag", ["is_nft_op", "is_registration", "is_deploy", "is_journal_entry"])
def test_a_claim_that_is_also_another_shape_refuses(flag):
    kind, why = S.gate(_intent(**{flag: True}), _tx(), HOLDER)
    assert kind is None and "cannot also be" in why


def test_a_claim_through_an_account_refuses():
    kind, why = S.gate(_intent(via_account=ACCOUNT, via_account_state=1), _tx(), HOLDER)
    assert kind is None and "account call" in why


def test_the_signed_destination_must_be_the_sale():
    kind, why = S.gate(_intent(), _tx(to=OTHER), HOLDER)
    assert kind is None and "addressed to it" in why


# --- the settle's structure and pins ------------------------------------------------------

@pytest.mark.parametrize("ids,match", [((4, 1), "ascending"), ((1, 1), "ascending"),
                                       ((), "at least one")])
def test_a_malformed_record_list_refuses(ids, match):
    kind, why = S.gate(_intent(), _tx(S.encode_settle(list(ids))), HOLDER)
    assert kind is None and match in why


def test_a_settle_before_the_sale_is_settled_refuses():
    rpc = Sale(settled=False)
    intent, deltas, tx = _settle(rpc=rpc)
    d = _authorize(intent, deltas, tx=tx, rpc=rpc)
    assert d.allowed is False and "not settled" in d.reason


def test_a_settle_of_another_buyers_record_refuses():
    intent, deltas, tx = _settle(ids=(1, 2))
    d = _authorize(intent, deltas, tx=tx)
    assert d.allowed is False and "not this treasury" in d.reason


def test_a_settle_of_a_done_record_refuses():
    intent, deltas, tx = _settle(ids=(3,))
    d = _authorize(intent, _deltas(native=1, logs=[]), tx=tx)
    assert d.allowed is False and "already settled" in d.reason


def test_an_unreadable_record_refuses():
    rpc = Sale(unreadable={4})
    intent, deltas, tx = _settle(rpc=rpc)
    d = _authorize(intent, deltas, tx=tx, rpc=rpc)
    assert d.allowed is False and "could not be read" in d.reason


def test_a_changed_sale_code_refuses(monkeypatch):
    pin(monkeypatch, profile(COLLECTION, sale_pin=sale(COLLECTION, runtime_sha256="ab" * 32)))
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "sale" in d.reason and "runtime" in d.reason.lower()


# --- the measurement -----------------------------------------------------------------------

def test_a_claim_that_returns_less_than_owed_refuses():
    d = _authorize(_intent(), _deltas(native=OWED - 1, logs=[_claimed(amount=OWED - 1)]))
    assert d.allowed is False and "below the declared minimum" in d.reason


def test_a_claim_with_no_claimed_event_refuses():
    d = _authorize(_intent(), _deltas(logs=[]))
    assert d.allowed is False and "exactly one Claimed" in d.reason


def test_a_claimed_event_for_another_address_refuses():
    d = _authorize(_intent(), _deltas(logs=[_claimed(who=OTHER)]))
    assert d.allowed is False and "exactly one Claimed" in d.reason


def test_a_claim_that_moves_a_token_refuses():
    d = _authorize(_intent(), _deltas(token_deltas={USDC: 5}))
    assert d.allowed is False and "beyond the native receipt" in d.reason


def test_a_claim_that_changes_an_allowance_refuses():
    d = _authorize(_intent(), _deltas(allowance_deltas={(USDC, OTHER): -1}))
    assert d.allowed is False and "allowance" in d.reason


def test_a_claim_that_receives_an_nft_refuses():
    d = _authorize(_intent(), _deltas(holder_nft_in=((COLLECTION, "erc721", COLLECTION, 1, 1),)))
    assert d.allowed is False and "NFT arrivals" in d.reason


def test_a_log_from_another_contract_refuses():
    d = _authorize(_intent(), _deltas(logs=[_claimed(), _log("0x" + "ab" * 32, address=OTHER)]))
    assert d.allowed is False and "every log of a claim" in d.reason


def test_a_log_naming_the_signer_refuses():
    d = _authorize(_intent(), _deltas(logs=[_claimed(), _log(S.TOPIC_OWED, topics=[
        "0x" + _aw(HOLDER)])]))
    assert d.allowed is False and "names the signer" in d.reason


def test_another_sale_event_in_a_claim_refuses():
    d = _authorize(_intent(), _deltas(logs=[_claimed(), _settled(1, 5)]))
    assert d.allowed is False and "not part of a claim" in d.reason


def test_a_settle_that_skips_a_declared_record_refuses():
    intent, deltas, tx = _settle()
    d = _authorize(intent, _deltas(native=deltas.native_delta, logs=deltas.logs[:1]), tx=tx)
    assert d.allowed is False and "one Settled" in d.reason


def test_a_settle_whose_refunds_are_below_the_minimum_refuses():
    intent, deltas, tx = _settle()
    logs = [_settled(1, 1), _settled(4, 1)]
    d = _authorize(intent, _deltas(native=deltas.native_delta, logs=logs), tx=tx)
    assert d.allowed is False and "refunds" in d.reason


def test_a_settle_that_books_the_treasurys_refund_in_owed_refuses():
    intent, deltas, tx = _settle()
    logs = [*deltas.logs, _log(S.TOPIC_OWED, _aw(HOLDER), _w(5))]
    d = _authorize(intent, _deltas(native=deltas.native_delta, logs=logs), tx=tx)
    assert d.allowed is False and "Owed(treasury)" in d.reason


def test_a_settle_that_pays_less_native_than_declared_refuses():
    intent, deltas, tx = _settle()
    d = _authorize(intent, _deltas(native=deltas.native_delta - 1, logs=deltas.logs), tx=tx)
    assert d.allowed is False and "below the declared minimum" in d.reason


def test_a_claim_that_sends_native_out_refuses():
    d = _authorize(_intent(), _deltas(native=-10 ** 15))
    assert d.allowed is False and "OUT of the wallet" in d.reason
