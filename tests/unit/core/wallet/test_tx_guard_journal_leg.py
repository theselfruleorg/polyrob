"""J1 (polyrob-desk handoff-core) — the account's journal entry rides the action's executeBatch.

``TxIntent.via_account_journal``: the LAST leg is ``JournalLog.log(entry)``. The guard allows
exactly that leg — the ``journal_log`` pinned for the account's collection, ``log(bytes)``,
value 0, operation 0, one canonical entry of THIS account and chain signed by the signing
treasury — and judges the rest as the plain ``execute`` (or the W8 batch) without it.
"""
import dataclasses
import os

import pytest
from eth_account import Account

from core.wallet import collection_registry, erc6551, journal_log, nft_account, tx_guard
from core.wallet.signer import LocalEoaSigner
from tests.unit.core.wallet.test_tx_guard_via_account import (
    ACCOUNT, BASE_ID, STATE, TO, AccountRpc, _erc20_deltas, _erc20_inner, _erc20_intent,
    _pins, _profile, _run)

assert _pins  # the code-pin fixture (autouse) from the via_account tests

JLOG = "0x10910910910910910910910910910910910910a1"
OTHER_JLOG = "0x10910910910910910910910910910910910910b2"


@pytest.fixture
def key():
    return LocalEoaSigner(bytes(Account.create().key))


@pytest.fixture(autouse=True)
def _journal_pinned(monkeypatch):
    monkeypatch.setattr(collection_registry, "profiles",
                        lambda: (_profile(journal_log=JLOG.lower()),))


def _entry(key, **over):
    e = nft_account.build_entry(prior=[], account=ACCOUNT, chain_id=BASE_ID, kind="tend",
                                text="transfer 0.25 USDC", owner=key.address)
    e.update(over)
    return nft_account.sign_entry(e, key) if "sig" not in over else e


def _leg(entry, *, to=JLOG, value=0, op=0, raw=None):
    data = journal_log.encode_log(raw if raw is not None else nft_account.canonical(entry))
    return (to, value, data, op)


def _outer(legs):
    return {"to": ACCOUNT, "value": 0, "chainId": 8453, "nonce": 5, "gas": 300_000,
            "maxFeePerGas": 10 ** 8, "data": erc6551.encode_execute_batch(legs)}


def _action_leg():
    i = _erc20_inner()
    return (i["to"], i["value"], i["data"], 0)


def _intent(**kw):
    base = dict(via_account=ACCOUNT, via_account_state=STATE, via_account_journal=True)
    base.update(kw)
    return dataclasses.replace(_erc20_intent(), **base)


def _go(key, legs, intent=None, rpc=None):
    return _run(intent or _intent(), _erc20_deltas(), _outer(legs), holder=key.address,
                rpc=rpc or AccountRpc(owner=key.address))


def test_an_action_and_its_entry_in_one_batch_is_allowed(key):
    d = _go(key, [_action_leg(), _leg(_entry(key))])
    assert d.allowed is True and d.lane == "autonomous", d.reason


def test_the_verdict_equals_the_plain_execute(key):
    """The journal leg changes nothing about how the action is judged."""
    from tests.unit.core.wallet.test_tx_guard_via_account import _outer as plain_outer, _via
    plain = _run(_via(_erc20_intent()), _erc20_deltas(), plain_outer(_erc20_inner()),
                 holder=key.address, rpc=AccountRpc(owner=key.address))
    journaled = _go(key, [_action_leg(), _leg(_entry(key))])
    assert (plain.allowed, plain.lane, plain.amount_usd) == \
        (journaled.allowed, journaled.lane, journaled.amount_usd)


@pytest.mark.parametrize("make_legs, needle", [
    (lambda k: [_action_leg(), _leg(_entry(k), to=OTHER_JLOG)], "no pinned JournalLog"),
    (lambda k: [_action_leg(), _leg(_entry(k), value=1)], "no value"),
    (lambda k: [_action_leg(), _leg(_entry(k), op=1)], "operation 0"),
    (lambda k: [_action_leg()], "exactly 2 legs"),
    (lambda k: [_action_leg(), _leg(_entry(k)), _leg(_entry(k))], "exactly 2 legs"),
    (lambda k: [_action_leg(), _leg(None, raw=b"hello")], "not one canonical journal entry"),
    (lambda k: [_action_leg(), _leg(None, raw=nft_account.canonical(_entry(k)) + b" ")],
     "not one canonical journal entry"),
    (lambda k: [_action_leg(), _leg(_entry(k, account=TO.lower()))], "another account"),
    (lambda k: [_action_leg(), _leg(_entry(k, chain_id=4663))], "another account, chain"),
    (lambda k: [_action_leg(), _leg(_entry(k, sig="0x" + "11" * 65))], "does not recover"),
    # a valid entry, signed by a DIFFERENT key than the one sending
    (lambda k: [_action_leg(), _leg(_entry(LocalEoaSigner(os.urandom(32))))], "another account"),
    # the journal leg placed first: the action leg is then judged as the "journal"
    (lambda k: [_leg(_entry(k)), _action_leg()], "no pinned JournalLog"),
])
def test_journal_leg_refusals(key, make_legs, needle):
    d = _go(key, make_legs(key))
    assert d.allowed is False and needle in d.reason, d.reason


def test_a_text_edited_after_signing_does_not_recover(key):
    e = _entry(key)
    e["text"] = "something else"
    d = _go(key, [_action_leg(), _leg(e)])
    assert d.allowed is False and "does not recover" in d.reason


def test_the_flag_needs_an_account():
    intent = dataclasses.replace(_erc20_intent(), via_account_journal=True)
    d = _run(intent, _erc20_deltas(), dict(_erc20_inner(), chainId=8453, nonce=1, gas=1,
                                           maxFeePerGas=1), holder=TO)
    assert d.allowed is False and "declare `via_account`" in d.reason


def test_an_undeclared_journal_batch_stays_account_admin(key):
    """Without the flag a two-leg executeBatch is still the account-admin call it always was."""
    intent = dataclasses.replace(_intent(), via_account_journal=False)
    d = _go(key, [_action_leg(), _leg(_entry(key))], intent=intent)
    assert d.allowed is False and "executeBatch" in d.reason, d.reason


def test_the_leg_must_be_the_journal_of_the_accounts_own_collection(key, monkeypatch):
    """Two collections pin two JournalLogs: an account of A may not write to B's."""
    b = "0x7777777777777777777777777777777777777777"
    monkeypatch.setattr(collection_registry, "profiles", lambda: (
        _profile(journal_log=JLOG.lower()),
        _profile(address=b, journal_log=OTHER_JLOG.lower())))
    d = _go(key, [_action_leg(), _leg(_entry(key), to=OTHER_JLOG)])
    assert d.allowed is False and "not the JournalLog pinned for" in d.reason, d.reason


def test_no_journal_log_pinned_refuses_a_journal_leg(key, monkeypatch):
    monkeypatch.setattr(collection_registry, "profiles", lambda: (_profile(),))
    d = _go(key, [_action_leg(), _leg(_entry(key))])
    assert d.allowed is False and "no pinned JournalLog" in d.reason


def test_the_w8_batch_carries_the_entry_as_a_fourth_leg(key):
    from tests.unit.core.wallet.test_tx_guard_via_account import (_batch_deltas, _batch_intent,
                                                                  _good_legs)
    intent = dataclasses.replace(_batch_intent(), via_account_journal=True)
    rpc = AccountRpc(owner=key.address)
    d = _run(intent, _batch_deltas(), _outer(_good_legs() + [_leg(_entry(key))]),
             holder=key.address, rpc=rpc)
    assert d.allowed is True, d.reason
    d = _run(intent, _batch_deltas(), _outer(_good_legs()), holder=key.address, rpc=rpc)
    assert d.allowed is False and "exactly 4 legs" in d.reason


# --- a journal entry with NO action (is_journal_entry) ----------------------------------------

def _only_intent(**kw):
    base = dict(chain="base", token=None, to=JLOG, amount_raw=0, max_spend_usd=1.0,
                idempotency_key="j", via_account=ACCOUNT, via_account_state=STATE,
                is_journal_entry=True)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _only_tx(entry, to=JLOG, value=0):
    data = journal_log.encode_log(nft_account.canonical(entry))
    return {"to": ACCOUNT, "value": 0, "chainId": 8453, "nonce": 5, "gas": 100_000,
            "maxFeePerGas": 10 ** 8, "data": erc6551.encode_execute(to, value, data, 0)}


def _only_deltas(**kw):
    from core.wallet.simulation import Deltas
    base = dict(ok=True, gas_used=40_000, event_topics=((JLOG.lower(), journal_log.TOPIC_ENTRY),))
    base.update(kw)
    return Deltas(**base)


def _only(key, intent=None, tx=None, deltas=None):
    return _run(intent or _only_intent(), deltas or _only_deltas(), tx or _only_tx(_entry(key)),
                holder=key.address, rpc=AccountRpc(owner=key.address))


def test_a_journal_only_entry_is_allowed(key):
    d = _only(key)
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("make, needle", [
    (lambda k: dict(deltas=_only_deltas(event_topics=())), "does not show the JournalLog emit"),
    (lambda k: dict(deltas=_only_deltas(native_delta=-1)), "moved something"),
    (lambda k: dict(deltas=_only_deltas(holder_transfers=((TO.lower(), TO.lower(), 1),))),
     "moved something"),
    (lambda k: dict(intent=_only_intent(amount_raw=1)), "moves nothing"),
    (lambda k: dict(intent=_only_intent(to=OTHER_JLOG), tx=_only_tx(_entry(k), to=OTHER_JLOG)),
     "no pinned JournalLog"),
    (lambda k: dict(tx=_only_tx(_entry(k), to=OTHER_JLOG)), "declared JournalLog"),
    (lambda k: dict(tx=_only_tx(_entry(k, sig="0x" + "11" * 65))), "does not recover"),
    (lambda k: dict(intent=_only_intent(via_account=None, via_account_state=None)),
     "declare `via_account`"),
])
def test_journal_only_refusals(key, make, needle):
    d = _only(key, **make(key))
    assert d.allowed is False and needle in d.reason, d.reason
