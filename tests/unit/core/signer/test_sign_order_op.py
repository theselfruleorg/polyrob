"""111 §8.3 — ``sign.order``: the signer signs a class-O market Listing whose facts it READ
ITSELF (its own RPC, the registry), with the same classifier as the agent.

Nothing on the wire is a fact: a payload the signer's own reads do not bind refuses; the signer's
spend pause stops it (a money op); a daily cap bounds it; ``verdict_only`` signs nothing; the
remote and shadow signers route ``sign_market_order`` through the op. Every signature needs the
owner's independent on-box approval of THAT listing digest (the ``evm.send`` NFT rule): none →
refused with one pending row; granted → signed once; a second call needs a new approval; a grant
of one digest does not sign another; ``verdict_only`` opens nothing.
"""
import pytest
from eth_account import Account

from core.signer import protocol, sign_ops
from core.signer.remote import RemoteEvmSigner
from core.wallet.sign_policy import SignRefused, SignRequest, sign_market_order
from tests.unit.core.signer.conftest import LoopbackClient
from tests.unit.core.signer.test_066_p2_signer_schemas import _ok, _refused
from tests.unit.core.wallet import test_sale_orders as T


@pytest.fixture
def order_rig(rig, monkeypatch):
    """The signer's treasury owns the token; the signer reads the fake chain by its own RPC."""
    me = rig.service.wallet.operational_signer().address.lower()
    monkeypatch.setattr(T, "HOLDER", me)
    rpc = T.MarketRpc(owner=me)
    rig.service.order_rpc_fn = lambda chain: rpc
    for name, value in (("profiles", lambda: (T.P._profile(),)),):
        monkeypatch.setattr(T.collection_registry, name, value)
    monkeypatch.setattr(T.erc6551, "CODE_SHA256", {T.erc6551.REGISTRY.lower(): T.P._H00,
                                                   T.erc6551.ACCOUNT_V3_IMPL.lower(): "f" * 64})
    rig.me, rig.rpc = me, rpc
    return rig


def _typed(rig, **kw):
    return T._typed(T._listing(seller=rig.me, promiseHash=T.S.conditions_hash(T._promise(rig.rpc)),
                               **kw))


def _decisions(rig):
    return [r for r in rig.service.store.recent_decisions() if r["op"] == "sign.order"]


def _pending(rig):
    return [r for r in rig.service.store.pending() if r["op"] == "sign.order"]


def _approved(rig, typed):
    """Ask once (refused, one approval opened), grant it as root, return the body."""
    body = {"chain": "base", "payload": typed}
    resp = _refused(rig.call("sign.order", body), protocol.APPROVAL_REQUIRED)
    _ok(rig.call("approvals.decide", {"id": resp["approval_id"], "grant": True}, uid=0))
    rig.last_approval = resp["approval_id"]
    return body


def test_no_approval_refuses_and_opens_one_pending_row(order_rig):
    typed = _typed(order_rig)
    resp = _refused(order_rig.call("sign.order", {"chain": "base", "payload": typed}),
                    protocol.APPROVAL_REQUIRED)
    assert "requires independent signer approval" in resp["reason"]
    assert f"sudo polyrob owner promote signer_approval {resp['approval_id']}" in resp["reason"]
    (row,) = _pending(order_rig)
    assert row["id"] == resp["approval_id"] and row["digest"] == resp["digest"]
    assert "LISTING" in row["summary"] and "#1" in row["summary"]
    again = _refused(order_rig.call("sign.order", {"chain": "base", "payload": typed}),
                     protocol.APPROVAL_REQUIRED)
    assert again["approval_id"] == resp["approval_id"] and len(_pending(order_rig)) == 1
    assert "signature" not in resp


def test_the_signer_signs_a_bound_listing_once_after_the_owners_approval(order_rig):
    typed = _typed(order_rig)
    body = _approved(order_rig, typed)
    out = _ok(order_rig.call("sign.order", body))
    assert out["cls"] == "O" and out["address"].lower() == order_rig.me
    who = Account._recover_hash(bytes.fromhex(out["digest"][2:]), signature=out["signature"])
    assert who.lower() == order_rig.me
    row = _decisions(order_rig)[0]
    assert row["allowed"] == 1 and out["signature"] not in str(row)
    # one use: the same digest needs a NEW approval
    resp = _refused(order_rig.call("sign.order", body), protocol.APPROVAL_REQUIRED)
    assert resp["approval_id"] != order_rig.last_approval
    assert len(_pending(order_rig)) == 1


def test_a_grant_of_one_listing_does_not_sign_another(order_rig):
    _approved(order_rig, _typed(order_rig))
    other = _typed(order_rig, salt=4242)
    resp = _refused(order_rig.call("sign.order", {"chain": "base", "payload": other}),
                    protocol.APPROVAL_REQUIRED)
    assert len(_pending(order_rig)) == 1 and resp["approval_id"] == _pending(order_rig)[0]["id"]


def test_a_grant_that_is_no_longer_live_signs_nothing(order_rig, monkeypatch):
    body = _approved(order_rig, _typed(order_rig))
    monkeypatch.setattr(order_rig.service.store, "consume_grant", lambda _id: False)
    resp = _refused(order_rig.call("sign.order", body), protocol.APPROVAL_REQUIRED)
    assert "no longer live" in resp["reason"] and "signature" not in resp


def test_another_ops_grant_never_unlocks_an_order(order_rig):
    typed = _typed(order_rig)
    digest = _refused(order_rig.call("sign.order", {"chain": "base", "payload": typed}),
                      protocol.APPROVAL_REQUIRED)["digest"]
    store = order_rig.service.store
    with store._db() as db:
        db.execute("UPDATE approvals SET op='evm.send', state='granted' WHERE digest=?", (digest,))
    _refused(order_rig.call("sign.order", {"chain": "base", "payload": typed}),
             protocol.APPROVAL_REQUIRED)


@pytest.mark.parametrize("kw, rpc_kw, needle", [
    (dict(price=0, maxRoyalty=0), {}, "not above 0"),
    ({}, dict(tc=9), "transferCount"),
    ({}, dict(digest_off=True), "disagree"),
    ({}, dict(market_code="0x6080"), "pinned market"),
])
def test_the_signer_refuses_what_its_own_reads_do_not_bind(order_rig, kw, rpc_kw, needle):
    for k, v in rpc_kw.items():
        setattr(order_rig.rpc, k, v)
    resp = _refused(order_rig.call("sign.order", {"chain": "base",
                                                  "payload": _typed(order_rig, **kw)}),
                    protocol.GUARD_REFUSED)
    assert needle in resp["reason"] and resp["cls"] == "D", resp
    assert _decisions(order_rig)[0]["allowed"] == 0


def test_a_listing_for_another_seller_refuses_in_the_signer(order_rig):
    typed = T._typed(T._listing(seller="0x" + "5e" * 20))
    resp = _refused(order_rig.call("sign.order", {"chain": "base", "payload": typed}),
                    protocol.GUARD_REFUSED)
    assert "not this treasury" in resp["reason"]


def test_an_offer_or_a_seaport_order_refuses_by_name(order_rig):
    typed = _typed(order_rig)
    typed["primaryType"] = "Offer"
    resp = _refused(order_rig.call("sign.order", {"chain": "base", "payload": typed}),
                    protocol.GUARD_REFUSED)
    assert "an Offer is not signed" in resp["reason"]


def test_the_spend_pause_stops_an_order(order_rig):
    assert order_rig.call("pause.set", {"paused": True}, uid=0)["ok"]
    _refused(order_rig.call("sign.order", {"chain": "base", "payload": _typed(order_rig)}),
             protocol.PAUSED)


def test_the_daily_cap(order_rig, monkeypatch):
    monkeypatch.setattr(sign_ops, "SIGN_ORDER_DAILY_CAP", 1)
    _ok(order_rig.call("sign.order", _approved(order_rig, _typed(order_rig))))
    resp = _refused(order_rig.call("sign.order", _approved(order_rig, _typed(order_rig, salt=7))),
                    protocol.GUARD_REFUSED)
    assert "market orders in 24 h" in resp["reason"]


def test_verdict_only_signs_nothing_and_opens_nothing(order_rig):
    out = _ok(order_rig.call("sign.order", {"chain": "base", "payload": _typed(order_rig),
                                            "verdict_only": True}))
    assert "signature" not in out and out["cls"] == "O"
    assert _pending(order_rig) == []


def test_unknown_keys_and_a_chain_off_the_policy_are_refused(order_rig):
    _refused(order_rig.call("sign.order", {"chain": "base", "payload": _typed(order_rig),
                                           "facts": {}}), protocol.UNKNOWN_SHAPE)
    _refused(order_rig.call("sign.order", {"chain": "ethereum", "payload": _typed(order_rig)}),
             protocol.CHAIN_NOT_ALLOWED)


def test_the_agent_side_remote_signer_surfaces_the_approval_then_signs(order_rig):
    from core.signer.client import SignerRefused
    typed = _typed(order_rig)
    signer = RemoteEvmSigner(LoopbackClient(order_rig.service), order_rig.me, "base",
                             verified_fn=lambda: True)
    facts = T.O.read_listing_facts(order_rig.rpc, T.collection_registry, chain="base",
                                   payload=typed, holder=order_rig.me)
    with pytest.raises(SignerRefused) as exc:
        sign_market_order(signer, SignRequest("eip712", typed), facts=facts, chain="base")
    assert exc.value.code == protocol.APPROVAL_REQUIRED
    aid = exc.value.extra["approval_id"]
    _ok(order_rig.call("approvals.decide", {"id": aid, "grant": True}, uid=0))
    signer = RemoteEvmSigner(LoopbackClient(order_rig.service), order_rig.me, "base",
                             verified_fn=lambda: True)
    facts = T.O.read_listing_facts(order_rig.rpc, T.collection_registry, chain="base",
                                   payload=typed, holder=order_rig.me)
    sig, verdict = sign_market_order(signer, SignRequest("eip712", typed), facts=facts,
                                     chain="base")
    assert verdict.cls == "O"
    assert Account._recover_hash(bytes.fromhex(verdict.digest[2:]), signature=sig).lower() \
        == order_rig.me


def test_the_agent_refuses_before_asking_the_signer(order_rig):
    client = LoopbackClient(order_rig.service)
    signer = RemoteEvmSigner(client, order_rig.me, "base", verified_fn=lambda: True)
    typed = T._typed(T._listing(seller="0x" + "5e" * 20))
    facts = T.O.read_listing_facts(order_rig.rpc, T.collection_registry, chain="base",
                                   payload=typed, holder=order_rig.me)
    with pytest.raises(SignRefused):
        sign_market_order(signer, SignRequest("eip712", typed), facts=facts, chain="base")
    assert client.calls == []


def test_the_shadow_signer_compares_then_signs_locally(order_rig, monkeypatch):
    from core.signer import shadow
    logged = []
    monkeypatch.setattr(shadow, "log_comparison", lambda kind, **f: logged.append((kind, f)))
    local = order_rig.service.wallet.operational_signer()
    sh = shadow.ShadowEvmSigner(local, client=LoopbackClient(order_rig.service))
    typed = _typed(order_rig)
    facts = T.O.read_listing_facts(order_rig.rpc, T.collection_registry, chain="base",
                                   payload=typed, holder=order_rig.me)
    sig, verdict = sign_market_order(sh, SignRequest("eip712", typed), facts=facts, chain="base")
    assert logged and logged[0][0] == "agree"
    assert Account._recover_hash(bytes.fromhex(verdict.digest[2:]), signature=sig).lower() \
        == order_rig.me
