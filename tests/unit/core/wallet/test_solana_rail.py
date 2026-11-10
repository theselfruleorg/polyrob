"""The Solana broadcast rail. Phase 3.

Mirrors `EvmRail`'s job — build, size, sign, send, confirm — against a different
transaction model. What replaces `chainId` as the replay bound is the recent
BLOCKHASH: a Solana transaction is valid only for ~150 slots (about a minute),
so a stale one cannot be replayed later, but it also silently expires.
"""
import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from core.wallet.solana_rail import SolanaRail, SolanaBroadcastError


@pytest.fixture(autouse=True)
def isolated_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))


def signed_transaction():
    from solders.keypair import Keypair
    from solders.hash import Hash
    from solders.transaction import Transaction
    key = Keypair()
    tx = Transaction.new_signed_with_payer([], key.pubkey(), [key], Hash.default())
    return bytes(tx), str(tx.signatures[0])


class _Signer:
    address = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"

    def __init__(self):
        self.signed = []

    def sign_transaction(self, tx):
        self.signed.append(tx)
        return tx


def _rail(rpc=None, signer=None):
    return SolanaRail(signer=signer or _Signer(), rpc=rpc or (lambda m, p: {}))


def test_the_rail_refuses_a_chain_that_may_not_move_value():
    """Registry-driven, exactly like EvmRail. solana is money_enabled=False
    until Phase 3 is armed, so the rail must refuse to exist."""
    with pytest.raises(ValueError, match="read-only|money"):
        SolanaRail(signer=_Signer(), rpc=lambda m, p: {}, enforce_registry=True)


def test_a_missing_blockhash_is_an_error_not_a_guess():
    """Signing against a blockhash we invented is a transaction that can never
    land — and inventing one hides an RPC outage."""
    rail = _rail(rpc=lambda m, p: {})
    with pytest.raises(SolanaBroadcastError, match="blockhash"):
        rail.recent_blockhash()


def test_a_blockhash_is_read_from_the_rpc():
    h = "11111111111111111111111111111111"
    rail = _rail(rpc=lambda m, p: {"value": {"blockhash": h, "lastValidBlockHeight": 9}})
    assert rail.recent_blockhash() == h


def test_send_refuses_an_unsigned_transaction():
    rail = _rail()
    with pytest.raises(SolanaBroadcastError, match="signed|signature"):
        rail.send_raw(b"")


def test_a_failed_send_raises_rather_than_returning_a_fake_signature():
    def _rpc(method, params):
        if method == "sendTransaction":
            raise RuntimeError("blockhash not found")
        return {}
    with pytest.raises(SolanaBroadcastError):
        _rail(rpc=_rpc).send_raw(signed_transaction()[0])


def test_a_successful_send_returns_the_signature():
    raw, signature = signed_transaction()
    rail = _rail(rpc=lambda m, p: signature if m == "sendTransaction" else {})
    assert rail.send_raw(raw) == signature


def test_confirmation_reports_failure_honestly():
    """A landed-but-reverted transaction is NOT a success. On EVM that is
    status=0; here it is a non-null `err` on the confirmed status."""
    status = {"value": [{"confirmationStatus": "finalized",
                         "err": {"InstructionError": [0, "Custom"]}}]}
    ok, detail = _rail(rpc=lambda m, p: status).confirm("5xSig", attempts=1, delay=0)
    assert ok is False
    assert "err" in detail.lower() or "fail" in detail.lower()


def test_confirmation_succeeds_on_a_clean_finalized_status():
    status = {"value": [{"confirmationStatus": "finalized", "err": None}]}
    ok, _ = _rail(rpc=lambda m, p: status).confirm("5xSig", attempts=1, delay=0)
    assert ok is True


def test_an_unknown_signature_is_unconfirmed_not_failed():
    """`null` means the cluster has not seen it yet — which is 'unknown', and
    reporting it as failure invites a double-send."""
    ok, detail = _rail(rpc=lambda m, p: {"value": [None]}).confirm(
        "5xSig", attempts=1, delay=0)
    assert ok is False
    assert "unknown" in detail.lower() or "not yet" in detail.lower()


def test_ambiguous_send_survives_restart_and_blocks_other_rails():
    from core.wallet import submission_journal as journal
    from core.wallet.policy import PolicyGate
    raw, signature = signed_transaction()

    def rpc(method, params):
        assert journal.unresolved()[0]["tx_hash"] == signature
        raise TimeoutError("reply lost after acceptance")

    with pytest.raises(SolanaBroadcastError, match="outcome unknown") as info:
        _rail(rpc=rpc).send_raw(raw)
    assert info.value.signature == signature       # the caller stamps it
    assert journal.unresolved()[0]["tx_hash"] == signature
    gate = PolicyGate(max_per_tx_usd=100)
    assert not gate.check(venue="x402", amount_usd=1, idempotency_key=None).allowed
    with pytest.raises(ValueError, match="unaccounted"):
        _rail(rpc=rpc).send_raw(raw)


def test_sol_signature_case_preserved_until_durable_accounting(tmp_path):
    from core.wallet import submission_journal as journal
    from core.wallet.policy import PolicyGate
    from core.wallet.audit_sink import JsonlAuditSink
    raw, signature = signed_transaction()
    _rail(rpc=lambda m, p: signature).send_raw(raw)
    assert journal.unresolved()[0]["tx_hash"] == signature
    journal.mark_booked(signature.swapcase())
    assert journal.unresolved()
    gate = PolicyGate(max_per_tx_usd=100, audit_sink=JsonlAuditSink(str(tmp_path / "audit.jsonl")))
    gate.record(venue="defi", action="transfer", amount_usd=1,
                counterparty=None, idempotency_key=None, result_ref=signature)
    assert not journal.unresolved()


def test_failed_durable_intent_never_contacts_rpc(monkeypatch):
    from core.wallet import submission_journal as journal
    raw, _ = signed_transaction()
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(journal, "prepare", fail)
    with pytest.raises(OSError):
        _rail(rpc=lambda *args: pytest.fail("must not send")).send_raw(raw)


def test_rpc_cannot_substitute_transaction_identifier():
    raw, signature = signed_transaction()
    with pytest.raises(SolanaBroadcastError, match="expected signature"):
        _rail(rpc=lambda m, p: "different-signature").send_raw(raw)


# -- a definite node refusal is NOT SENT, never "outcome unknown" -----------

def _refusing(code, message, data=None):
    from core.wallet.solana_onchain import SolanaRpcError

    def rpc(method, params):
        if method == "sendTransaction":
            raise SolanaRpcError(code, message, data)
        return {}
    return rpc


@pytest.mark.parametrize("code,message,data", [
    (-32002, "Transaction simulation failed: Blockhash not found",
     {"err": "BlockhashNotFound", "logs": []}),
    (-32002, "Transaction simulation failed: Error processing Instruction 0",
     {"err": {"InstructionError": [0, {"Custom": 1}]}, "logs": []}),
    (-32003, "Transaction signature verification failure", None),
])
def test_a_preflight_refusal_releases_the_interlock_and_reads_as_not_sent(code, message, data):
    from core.wallet import submission_journal as journal
    from core.wallet.broadcast.evm import outcome_unknown
    from core.wallet.solana_rail import SolanaRejected
    raw, signature = signed_transaction()
    with pytest.raises(SolanaRejected, match="not sent") as info:
        _rail(rpc=_refusing(code, message, data)).send_raw(raw)
    assert not outcome_unknown(info.value)
    assert not journal.unresolved()


def test_already_processed_is_not_a_refusal():
    """`AlreadyProcessed` means the bytes LANDED: never "not sent"."""
    from core.wallet import submission_journal as journal
    raw, signature = signed_transaction()
    with pytest.raises(SolanaBroadcastError, match="outcome unknown"):
        _rail(rpc=_refusing(-32002, "Transaction simulation failed: This "
                            "transaction has already been processed",
                            {"err": "AlreadyProcessed"})).send_raw(raw)
    assert journal.unresolved()[0]["tx_hash"] == signature


@pytest.mark.parametrize("code", [-32005, -32603, -32000])
def test_other_node_errors_stay_unknown(code):
    from core.wallet import submission_journal as journal
    raw, signature = signed_transaction()
    with pytest.raises(SolanaBroadcastError, match="outcome unknown"):
        _rail(rpc=_refusing(code, "node is behind")).send_raw(raw)
    assert journal.unresolved()[0]["tx_hash"] == signature


def test_the_default_transport_raises_a_typed_rpc_error(monkeypatch):
    """`solana_onchain._rpc` carries the node's code, so the rail can tell a
    refusal from a lost reply; the wrapped error keeps it as its cause."""
    import io
    import json as _json
    from core.wallet import solana_onchain
    from core.wallet.solana_onchain import SolanaRpcError
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
        "code": -32002, "message": "Transaction simulation failed",
        "data": {"err": "BlockhashNotFound"}}}).encode()

    class _Resp(io.BytesIO):
        headers = {}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(solana_onchain.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(body))
    monkeypatch.setattr(solana_onchain, "rpc_url", lambda: "http://rpc.invalid")
    with pytest.raises(SolanaRpcError) as info:
        solana_onchain._rpc("sendTransaction", [])
    assert info.value.code == -32002
    assert isinstance(info.value, RuntimeError)
    from core.wallet import submission_journal as journal
    from core.wallet.solana_rail import SolanaRejected
    raw, _ = signed_transaction()
    with pytest.raises(SolanaRejected):
        SolanaRail(signer=_Signer()).send_raw(raw)
    assert not journal.unresolved()


# -- confirm(): "not seen" is only final once the blockhash has expired ------

def _status_then(statuses, valid):
    """An RPC whose signature statuses come from *statuses* (the last repeats)
    and whose isBlockhashValid answers come from *valid* (the last repeats)."""
    seen = {"status": list(statuses), "valid": list(valid), "calls": []}

    def rpc(method, params):
        seen["calls"].append((method, params))
        if method == "getSignatureStatuses":
            q = seen["status"]
            return {"value": [q.pop(0) if len(q) > 1 else q[0]]}
        if method == "isBlockhashValid":
            q = seen["valid"]
            return {"value": q.pop(0) if len(q) > 1 else q[0]}
        return {}
    return rpc, seen


def test_an_unseen_signature_is_expired_only_after_the_blockhash_expired():
    from core.wallet.solana_rail import confirmation_outcome
    rpc, seen = _status_then([None], [True, True, False])
    ok, detail = _rail(rpc=rpc).confirm("5xSig", attempts=1, delay=0,
                                        blockhash="Hash1111111111111111111111111111")
    assert ok is False and confirmation_outcome(ok, detail) == "expired"
    methods = [m for m, _ in seen["calls"]]
    assert methods.count("isBlockhashValid") == 3
    assert methods[-1] == "getSignatureStatuses"          # one final read after expiry
    assert seen["calls"][-1][1][1] == {"searchTransactionHistory": True}


def test_a_signature_that_lands_while_the_blockhash_is_valid_is_confirmed():
    rpc, _ = _status_then([None, None, {"confirmationStatus": "confirmed", "err": None}],
                          [True])
    ok, detail = _rail(rpc=rpc).confirm("5xSig", attempts=1, delay=0,
                                        blockhash="Hash1111111111111111111111111111")
    assert ok is True and detail == "confirmed"


def test_a_signature_that_lands_at_the_final_read_is_not_expired():
    from core.wallet.solana_rail import confirmation_outcome
    rpc, _ = _status_then([None, {"confirmationStatus": "finalized", "err": None}], [False])
    ok, detail = _rail(rpc=rpc).confirm("5xSig", attempts=1, delay=0,
                                        blockhash="Hash1111111111111111111111111111")
    assert confirmation_outcome(ok, detail) == "confirmed"


def test_a_blockhash_still_valid_at_the_bound_stays_unknown(monkeypatch):
    from core.wallet import solana_rail
    from core.wallet.solana_rail import confirmation_outcome
    clock = iter(range(0, 10_000, 50))
    monkeypatch.setattr(solana_rail.time, "monotonic", lambda: next(clock))
    rpc, _ = _status_then([None], [True])
    ok, detail = _rail(rpc=rpc).confirm("5xSig", attempts=1, delay=0,
                                        blockhash="Hash1111111111111111111111111111")
    assert confirmation_outcome(ok, detail) == "unknown"


def test_an_unreadable_validity_is_never_expired(monkeypatch):
    from core.wallet import solana_rail
    from core.wallet.solana_rail import confirmation_outcome
    clock = iter(range(0, 10_000, 50))
    monkeypatch.setattr(solana_rail.time, "monotonic", lambda: next(clock))

    def rpc(method, params):
        if method == "isBlockhashValid":
            raise RuntimeError("node down")
        return {"value": [None]}
    ok, detail = _rail(rpc=rpc).confirm("5xSig", attempts=1, delay=0,
                                        blockhash="Hash1111111111111111111111111111")
    assert confirmation_outcome(ok, detail) == "unknown"


def test_confirm_reads_the_blockhash_from_the_journal():
    """The verbs call confirm(signature) alone: the rail journaled the
    blockhash at send time and finds it there."""
    from core.wallet.solana_rail import confirmation_outcome
    raw, signature = signed_transaction()
    rpc, seen = _status_then([None], [False])

    def both(method, params):
        if method == "sendTransaction":
            return signature
        return rpc(method, params)
    rail = _rail(rpc=both)
    rail.send_raw(raw)
    ok, detail = rail.confirm(signature, attempts=1, delay=0)
    assert confirmation_outcome(ok, detail) == "expired"
    valid_calls = [p for m, p in seen["calls"] if m == "isBlockhashValid"]
    assert valid_calls and valid_calls[0][0] == "11111111111111111111111111111111"
