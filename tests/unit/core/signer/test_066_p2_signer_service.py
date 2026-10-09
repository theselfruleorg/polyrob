"""066 P2 — the rules polyrob-signer enforces, whatever the agent asks.

Every test drives :class:`core.signer.server.SignerService.handle` the way the
socket does (a request dict + the caller's UID). The guard is the REAL
``tx_guard.authorize``; only the network is faked (see conftest).
"""
import pytest

from core.signer import protocol
from tests.unit.core.signer.conftest import (AGENT_UID, AMOUNT, OTHER, SEED, FakeRail, native_deltas,
                                             native_intent, native_tx)


def _ok(resp):
    assert resp["ok"], resp
    return resp["result"]


def _refused(resp, code):
    assert resp["ok"] is False, resp
    assert resp["code"] == code, resp
    return resp


# -- shapes ------------------------------------------------------------------

@pytest.mark.parametrize("op", ["sign_message", "sign_typed_data", "sign_raw", "evm.sign"])
def test_an_unknown_op_is_refused(rig, op):
    _refused(rig.call(op, {}), protocol.UNKNOWN_SHAPE)
    assert FakeRail.sent == []


def test_a_wrong_schema_version_is_refused(rig):
    resp = rig.service.handle({"v": 2, "op": "ping", "body": {}}, AGENT_UID)
    _refused(resp, protocol.UNKNOWN_SHAPE)


def test_an_unknown_intent_field_is_refused(rig):
    body = rig.body()
    body["intent"]["recipient_override"] = "0x" + "9" * 40
    _refused(rig.call("evm.send", body), protocol.UNKNOWN_SHAPE)
    assert FakeRail.sent == []


def test_an_unknown_transaction_field_is_refused(rig):
    body = rig.body()
    body["tx"]["accessList"] = []
    _refused(rig.call("evm.send", body), protocol.UNKNOWN_SHAPE)


def test_an_unknown_body_field_is_refused(rig):
    body = rig.body()
    body["holder"] = "0x" + "9" * 40       # the agent may not pick the key
    _refused(rig.call("evm.send", body), protocol.UNKNOWN_SHAPE)


def test_a_wrong_peer_uid_is_refused_before_anything(rig):
    _refused(rig.call("identity", {}, uid=AGENT_UID + 1), protocol.PEER_REFUSED)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_spend_declarations_never_reach_signing(rig, value):
    body = rig.body()
    body["intent"]["max_spend_usd"] = value
    _refused(rig.call("evm.send", body), protocol.UNKNOWN_SHAPE)
    assert FakeRail.sent == []


def test_fee_only_asset_risk_needs_independent_signer_review(rig, monkeypatch):
    from core.wallet.tx_guard import Decision
    # The valuation seam reports a fee within the cap. That is not a valuation
    # of the NFT itself and must not suffice to unlock the remote key.
    monkeypatch.setattr(rig.service, "_authorize", lambda *a, **k: Decision(
        True, "guard passed", amount_usd=0.01))
    body = rig.body(intent=native_intent(is_nft_op=True))
    response = _refused(rig.call("evm.send", body), protocol.APPROVAL_REQUIRED)
    row = rig.service.store.find_approval(protocol.request_digest(body["intent"],
                                                                 protocol.tx_from_wire(body["tx"])))
    assert "UNPRICED" in row["summary"]
    assert response["approval_id"] == row["id"]
    assert FakeRail.sent == []


def test_agent_side_owner_queue_does_not_authorize_remote_signing(rig, monkeypatch):
    from core.wallet.tx_guard import Decision
    monkeypatch.setattr(rig.service, "_authorize", lambda *a, **k: Decision(
        False, "owner review required", lane="owner_queue", amount_usd=10.0))
    body = rig.body()
    response = _refused(rig.call("evm.send", body), protocol.APPROVAL_REQUIRED)
    assert FakeRail.sent == []
    aid = response["approval_id"]
    _refused(rig.call("approvals.decide", {"id": aid, "grant": True}), protocol.NOT_ROOT)
    _ok(rig.call("approvals.decide", {"id": aid, "grant": True}, uid=0))
    _ok(rig.call("evm.send", body))
    assert len(FakeRail.sent) == 1


def test_signer_review_retains_actual_transaction_and_assets(rig, monkeypatch):
    import hashlib
    import json
    from types import SimpleNamespace
    from core.wallet.tx_guard import Decision
    from core.signer.approvals import pending_items
    monkeypatch.setattr(rig.service, '_authorize', lambda *a, **k: Decision(
        False, 'review\nforged heading\x1b]52;bad', lane='owner_queue', amount_usd=10))
    collection = '0x' + '2' * 40
    actual = '0x' + '3' * 40
    data = '0xa9059cbb' + ('0' * 24 + OTHER[2:]) + f'{123:064x}'
    body = rig.body(intent=native_intent(is_nft_op=True, nft_out=((collection, 'erc721', 42, 1),)))
    body['tx'].update(to=actual, data=data)
    response = _refused(rig.call('evm.send', body), protocol.APPROVAL_REQUIRED)
    row = _ok(rig.call('approvals.list', {}))['pending'][0]
    review = json.loads(row['summary'])
    assert len(row['summary']) > 400 and row['id'] == response['approval_id']
    assert review['actual_contract'] == actual
    assert review['declared_intent']['to'] == body['intent']['to']
    assert review['declared_intent']['nft_out'] == [[collection, 'erc721', 42, 1]]
    assert review['calldata_sha256'] == hashlib.sha256(bytes.fromhex(data[2:])).hexdigest()
    assert review['calldata_words'][0].endswith(OTHER[2:].lower())
    assert '\x1b' not in row['summary']
    assert review['request_digest'] == protocol.request_digest(body['intent'], body['tx'])
    items = pending_items(SimpleNamespace(call=lambda op: {'pending': [row]}))
    assert row['summary'] in items[0]['preview']


def test_signer_review_refuses_oversize_instead_of_truncating(rig):
    from core.signer.review import MAX_REVIEW_CHARS
    with pytest.raises(ValueError, match='review limit'):
        rig.service.store.open_approval(digest='d', op='evm.send', chain='base', amount_usd=1,
                                       summary='x' * (MAX_REVIEW_CHARS + 1), ttl_sec=60)
    assert not rig.service.store.pending()


def test_signer_review_queue_is_bounded(rig):
    from core.signer.review import MAX_PENDING_APPROVALS, MAX_REVIEW_CHARS
    from core.signer.protocol import MAX_FRAME
    import json
    for n in range(MAX_PENDING_APPROVALS):
        rig.service.store.open_approval(digest=str(n), op='evm.send', chain='base', amount_usd=1,
                                       summary='\\' * MAX_REVIEW_CHARS, ttl_sec=60)
    with pytest.raises(ValueError, match='queue is full'):
        rig.service.store.open_approval(digest='overflow', op='evm.send', chain='base', amount_usd=1,
                                       summary='bounded review', ttl_sec=60)
    pending = rig.service.store.pending()
    assert len(pending) == MAX_PENDING_APPROVALS
    assert len(json.dumps({'ok': True, 'pending': pending}).encode()) < MAX_FRAME


# -- the happy path ------------------------------------------------------------

def test_a_guarded_send_is_signed_by_the_operational_key_and_booked(rig):
    result = _ok(rig.call("evm.send", rig.body()))
    assert result["amount_usd"] == pytest.approx(10.27)
    assert len(FakeRail.sent) == 1
    sent = FakeRail.sent[0]
    assert sent["hash"] == result["tx_hash"]
    assert sent["from"] == rig.service.wallet.operational_signer().address
    # booked in the SIGNER's own ledger
    ledger = rig.service.gate.audit_log
    assert [e["result_ref"] for e in ledger] == [result["tx_hash"]]


def test_the_guard_runs_inside_the_signer(rig):
    # The simulation disagrees with the declaration: twice the declared outflow.
    rig.deltas = native_deltas(native_delta=-2 * AMOUNT)
    resp = _refused(rig.call("evm.send", rig.body()), protocol.GUARD_REFUSED)
    assert "refused" in resp["reason"]
    assert FakeRail.sent == []


def test_a_chain_outside_signer_toml_is_refused(rig):
    from core.wallet import chains
    other = next(r for r in chains.evm_rows() if r.money_enabled and r.name != "base")
    body = rig.body(native_intent(chain=other.name), native_tx(chainId=other.chain_id))
    _refused(rig.call("evm.send", body), protocol.CHAIN_NOT_ALLOWED)


def test_a_fee_above_the_chain_ceiling_is_refused(rig):
    body = rig.body(tx=native_tx(gas=2_000_000, maxFeePerGas=10 ** 10))
    _refused(rig.call("evm.send", body), protocol.FEE_CEILING)


# -- nonces ------------------------------------------------------------------

def test_nonce_reuse_is_refused(rig):
    _ok(rig.call("evm.send", rig.body()))
    # The chain has not moved on (a replacement attempt at the same nonce).
    again = rig.body(native_intent(idempotency_key="native-2"), native_tx(nonce=7))
    _refused(rig.call("evm.send", again), protocol.NONCE_REUSE)
    assert len(FakeRail.sent) == 1


def test_a_nonce_the_chain_already_consumed_is_refused(rig):
    rig.nonces["next"] = 9
    _refused(rig.call("evm.send", rig.body(tx=native_tx(nonce=8))), protocol.NONCE_REUSE)
    assert FakeRail.sent == []


def test_a_nonce_ahead_of_the_chain_is_refused(rig):
    _refused(rig.call("evm.send", rig.body(tx=native_tx(nonce=12))), protocol.NONCE_GAP)


def test_the_next_nonce_after_a_send_is_signed(rig):
    _ok(rig.call("evm.send", rig.body()))
    rig.nonces["next"] = 8
    _ok(rig.call("evm.send", rig.body(native_intent(idempotency_key="native-2"),
                                      native_tx(nonce=8))))
    assert len(FakeRail.sent) == 2


def test_a_definitively_rejected_send_frees_its_nonce(rig, monkeypatch):
    from core.wallet.broadcast.evm import BroadcastError
    real = FakeRail.sign_and_send
    state = {"reject": True}

    def maybe_reject(self, tx):
        if state["reject"]:
            raise BroadcastError("the node rejected the transaction (insufficient funds)")
        return real(self, tx)
    monkeypatch.setattr(FakeRail, "sign_and_send", maybe_reject)
    _refused(rig.call("evm.send", rig.body()), protocol.BROADCAST_FAILED)
    state["reject"] = False
    _ok(rig.call("evm.send", rig.body(native_intent(idempotency_key="native-2"))))


def test_an_unknown_send_outcome_keeps_the_nonce_claimed(rig, monkeypatch):
    def lost(self, tx):
        raise TimeoutError("the response was lost")
    monkeypatch.setattr(FakeRail, "sign_and_send", lost)
    resp = _refused(rig.call("evm.send", rig.body()), protocol.INTERNAL)
    assert "UNKNOWN" in resp["reason"]
    assert rig.service.store.highest_nonce("base", rig.service.wallet.operational_signer().address) == 7


# -- hard caps -----------------------------------------------------------------

def test_the_hard_per_tx_cap_holds_whatever_the_agent_says(make_rig):
    rig = make_rig(per_tx_usd=5.0)          # $10 send, $5 hard cap
    # The agent declares a big budget; the signer does not read the agent's caps.
    body = rig.body(native_intent(max_spend_usd=10_000.0))
    resp = _refused(rig.call("evm.send", body), protocol.APPROVAL_REQUIRED)
    assert resp["approval_id"].startswith("sig-")
    assert "polyrob owner promote signer_approval" in resp["reason"]
    assert FakeRail.sent == []


def test_the_hard_daily_cap_holds(make_rig):
    rig = make_rig(per_tx_usd=50.0, daily_usd=15.0)
    _ok(rig.call("evm.send", rig.body()))
    rig.nonces["next"] = 8
    body = rig.body(native_intent(idempotency_key="native-2"), native_tx(nonce=8))
    _refused(rig.call("evm.send", body), protocol.APPROVAL_REQUIRED)
    assert len(FakeRail.sent) == 1


def test_only_root_can_grant_and_a_grant_is_one_shot(make_rig):
    rig = make_rig(per_tx_usd=5.0)
    resp = _refused(rig.call("evm.send", rig.body()), protocol.APPROVAL_REQUIRED)
    aid = resp["approval_id"]
    listed = _ok(rig.call("approvals.list", {}))["pending"]
    assert [r["id"] for r in listed] == [aid]
    # The agent UID (a Telegram /approve runs there) cannot decide it.
    _refused(rig.call("approvals.decide", {"id": aid, "grant": True}), protocol.NOT_ROOT)
    _refused(rig.call("evm.send", rig.body()), protocol.APPROVAL_REQUIRED)
    # The owner on the box (uid 0) can.
    assert _ok(rig.call("approvals.decide", {"id": aid, "grant": True}, uid=0))["state"] == "granted"
    _ok(rig.call("evm.send", rig.body()))
    assert len(FakeRail.sent) == 1
    # Consumed: the same request above the cap needs a NEW approval.
    rig.nonces["next"] = 8
    again = rig.body(native_intent(idempotency_key="native-2"), native_tx(nonce=8))
    _refused(rig.call("evm.send", again), protocol.APPROVAL_REQUIRED)


def test_a_grant_is_spent_when_the_send_outcome_is_unknown(make_rig, monkeypatch):
    """A send whose outcome is UNKNOWN may have left; its grant must not ride a retry.

    Before the fix the grant was consumed only after a booked success, so a lost
    response left it live for the rest of its TTL and the same above-cap request
    (a fresh nonce, once the journal was reconciled) went out a second time on
    ONE owner approval."""
    rig = make_rig(per_tx_usd=5.0)
    aid = _refused(rig.call("evm.send", rig.body()), protocol.APPROVAL_REQUIRED)["approval_id"]
    _ok(rig.call("approvals.decide", {"id": aid, "grant": True}, uid=0))
    real = FakeRail.sign_and_send

    def lost(self, tx):
        real(self, tx)
        raise TimeoutError("the response was lost")
    monkeypatch.setattr(FakeRail, "sign_and_send", lost)
    _refused(rig.call("evm.send", rig.body()), protocol.INTERNAL)
    monkeypatch.setattr(FakeRail, "sign_and_send", real)
    rig.nonces["next"] = 8
    again = rig.body(native_intent(idempotency_key="native-2"), native_tx(nonce=8))
    _refused(rig.call("evm.send", again), protocol.APPROVAL_REQUIRED)
    assert len(FakeRail.sent) == 1


def test_a_different_request_does_not_ride_a_grant(make_rig):
    rig = make_rig(per_tx_usd=5.0)
    aid = _refused(rig.call("evm.send", rig.body()), protocol.APPROVAL_REQUIRED)["approval_id"]
    _ok(rig.call("approvals.decide", {"id": aid, "grant": True}, uid=0))
    other = rig.body(native_intent(to=OTHER), native_tx(to=OTHER))
    _refused(rig.call("evm.send", other), protocol.APPROVAL_REQUIRED)
    assert FakeRail.sent == []


def test_the_pause_is_the_owners_and_binds(rig):
    _refused(rig.call("pause.set", {"paused": True}), protocol.NOT_ROOT)
    _ok(rig.call("pause.set", {"paused": True}, uid=0))
    _refused(rig.call("evm.send", rig.body()), protocol.PAUSED)
    _ok(rig.call("pause.set", {"paused": False}, uid=0))
    _ok(rig.call("evm.send", rig.body()))


def test_the_signer_ledger_survives_an_agent_side_ledger_deletion(make_rig, tmp_path):
    rig = make_rig(per_tx_usd=50.0, daily_usd=15.0)
    _ok(rig.call("evm.send", rig.body()))
    # The agent's own ledger is a different file; deleting it changes nothing here.
    agent_ledger = tmp_path / "agent" / "wallet" / "audit.jsonl"
    agent_ledger.parent.mkdir(parents=True)
    agent_ledger.write_text("")
    agent_ledger.unlink()
    rig.nonces["next"] = 8
    body = rig.body(native_intent(idempotency_key="native-2"), native_tx(nonce=8))
    _refused(rig.call("evm.send", body), protocol.APPROVAL_REQUIRED)


# -- shadow verdicts ------------------------------------------------------------

def test_a_verdict_never_signs_and_never_charges_the_real_ledger(rig):
    result = _ok(rig.call("evm.verdict", rig.body()))
    assert result["allowed"] is True
    assert FakeRail.sent == []
    assert rig.service.gate.audit_log == []
    assert len(rig.service.shadow_gate.audit_log) == 1


def test_a_verdict_reports_the_hard_cap_without_opening_an_approval(make_rig):
    rig = make_rig(per_tx_usd=5.0)
    _refused(rig.call("evm.verdict", rig.body()), protocol.APPROVAL_REQUIRED)
    assert _ok(rig.call("approvals.list", {}))["pending"] == []


# -- identity / continuity -------------------------------------------------------

def test_addresses_are_derived_identically_in_local_and_remote(rig, monkeypatch):
    from core.wallet.agent_wallet import VENUES, AgentWallet
    from core.wallet.config import WalletConfig
    local = AgentWallet(WalletConfig(enabled=True, backend="local_eoa", master_seed=SEED,
                                     network="mainnet", max_per_tx_usd=250.0,
                                     x402_client_enabled=False, x402_facilitator_url=""))
    ident = _ok(rig.call("identity", {}))
    assert ident["evm"] == {v: local.signer_for(v).address for v in sorted(VENUES)}
    assert ident["solana"] == local.solana_address
    assert ident["scheme"] == "legacy"


def test_deposit_addresses_match_the_agent_side_generator(rig):
    from modules.payments.wallet_generator import DepositWalletGenerator
    got = _ok(rig.call("deposit.address", {"user_id": "u-1"}))["address"]
    assert got == DepositWalletGenerator("p" * 40).generate_deposit_address("u-1")


def test_a_signer_on_a_different_seed_refuses_to_start(signer_home):
    from tests.unit.core.signer.conftest import make_config
    from core.signer import runtime
    cfg = make_config(signer_home, identity={"treasury": "0x" + "1" * 40})
    with pytest.raises(runtime.SignerStartupError, match="continuity"):
        runtime.build_service(cfg, secret_fn=lambda n: SEED if n == "AGENT_WALLET_MASTER_SEED" else None)


def test_the_signer_starts_hardened_before_it_takes_the_seed(signer_home, monkeypatch):
    from core.security import custody_env, process_hardening as ph
    from core.signer import runtime
    from tests.unit.core.signer.conftest import make_config
    order = []
    monkeypatch.setattr(ph, "_is_linux", lambda: True)
    monkeypatch.setattr(custody_env, "take_custody_secrets", lambda: order.append("take"))

    def harden():
        order.append("harden")
        return ph.HardeningResult(ph.STATE_HELD, "test")
    runtime.prepare_process(make_config(signer_home), harden=harden)
    assert order == ["harden", "take"]
    # A dumpable signer on Linux refuses to start.
    with pytest.raises(runtime.SignerStartupError, match="non-dumpable"):
        runtime.prepare_process(make_config(signer_home),
                                harden=lambda: ph.HardeningResult(ph.STATE_FAILED, "no prctl"))
