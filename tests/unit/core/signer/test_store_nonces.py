"""098 W1 CO-22: the signer's nonce journal is a machine with an exit.

An unknown send outcome marks the nonce ``unknown``; the ROOT
``nonce.reconcile`` op decides it from the chain (taken → ``sent``, the chain
still waits on it → ``released`` and re-usable). ``bind_nonce`` is a CAS, and
the approval dedup read is inside the write transaction."""
import threading

import pytest

from core.signer import protocol
from core.signer.store import NONCE, SignerStore

from tests.unit.core.signer.conftest import FakeRail, native_intent

A = "0x" + "c" * 40


def _ok(resp):
    assert resp["ok"], resp
    return resp["result"]


def _lost(monkeypatch):
    def lost(self, tx):
        raise TimeoutError("the response was lost")
    monkeypatch.setattr(FakeRail, "sign_and_send", lost)


def _row(rig, nonce=7):
    holder = rig.service.wallet.operational_signer().address.lower()
    with rig.service.store._db() as db:
        r = db.execute("SELECT * FROM nonces WHERE chain='base' AND address=? AND nonce=?",
                       (holder, nonce)).fetchone()
    return dict(r) if r else None


def test_unknown_outcome_then_reconcile_releases_when_not_on_chain(rig, monkeypatch):
    _lost(monkeypatch)
    rig.call("evm.send", rig.body())
    assert _row(rig)["state"] == "unknown"
    # every later send is refused while it stands
    monkeypatch.undo()
    resp = rig.call("evm.send", rig.body(native_intent(idempotency_key="n2")))
    assert resp["code"] == protocol.NONCE_REUSE
    rig.service.RECONCILE_GRACE_SEC = 0
    out = _ok(rig.call("nonce.reconcile", {"chain": "base"}, uid=0))
    assert out["nonces"] == [{"nonce": 7, "state": "released", "left": False}]
    assert _row(rig)["state"] == "released"   # history kept, not deleted
    _ok(rig.call("evm.send", rig.body(native_intent(idempotency_key="n3"))))
    assert _row(rig)["state"] == "sent"


def test_reconcile_marks_sent_when_receipt_found(rig, monkeypatch):
    _lost(monkeypatch)
    rig.call("evm.send", rig.body())
    rig.nonces["next"] = 8   # the chain took nonce 7
    out = _ok(rig.call("nonce.reconcile", {"chain": "base"}, uid=0))
    assert out["nonces"][0]["state"] == "sent"


def test_reconcile_inside_grace_leaves_the_row(rig, monkeypatch):
    _lost(monkeypatch)
    rig.call("evm.send", rig.body())
    out = _ok(rig.call("nonce.reconcile", {"chain": "base"}, uid=0))
    assert out["nonces"][0]["left"] is True and _row(rig)["state"] == "unknown"


def test_reconcile_is_root_only(rig):
    resp = rig.call("nonce.reconcile", {"chain": "base"})
    assert resp["ok"] is False and resp["code"] == protocol.NOT_ROOT


def test_bind_nonce_requires_reserved(tmp_path):
    store = SignerStore(str(tmp_path))
    assert not store.bind_nonce("base", A, 3, "0xabc")   # no row
    assert store.reserve_nonce("base", A, 3)
    assert store.release_nonce("base", A, 3)
    assert not store.bind_nonce("base", A, 3, "0xabc")   # released, not reserved
    assert store.reserve_nonce("base", A, 3)              # the released row is re-used
    assert store.bind_nonce("base", A, 3, "0xabc")
    assert not store.release_nonce("base", A, 3)          # sent is terminal
    assert not NONCE.validate()


def test_open_approval_dedup_inside_txn(tmp_path):
    store = SignerStore(str(tmp_path))
    rows, start = [], threading.Barrier(4)

    def open_one():
        start.wait()
        rows.append(store.open_approval(digest="d1", op="evm.send", chain="base",
                                        amount_usd=10.0, summary="s", ttl_sec=600)["id"])
    threads = [threading.Thread(target=open_one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(rows)) == 1
    with store._db() as db:
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1


def test_reconcile_decides_a_deposit_address_nonce_too(rig):
    """098 review O-12: a deposit sweep with an unknown outcome leaves a nonce
    on the DEPOSIT address; nonce.reconcile decides it as well (it only read the
    operational address, so that address's later sweeps hit NONCE_REUSE)."""
    dep = "0x" + "d" * 40
    store = rig.service.store
    assert store.reserve_nonce("base", dep, 3)
    assert store.mark_nonce_unknown("base", dep, 3)
    rig.service.RECONCILE_GRACE_SEC = 0
    rig.service._pending_nonce_fn = lambda chain, holder: 4 if holder.lower() == dep else 7
    out = _ok(rig.call("nonce.reconcile", {"chain": "base"}, uid=0))
    assert {"nonce": 3, "state": "sent", "left": False, "address": dep} in out["nonces"]
    assert store.unresolved_nonces("base", dep) == []
