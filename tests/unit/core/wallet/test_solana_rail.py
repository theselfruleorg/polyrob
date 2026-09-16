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

    with pytest.raises(SolanaBroadcastError, match="outcome unknown"):
        _rail(rpc=rpc).send_raw(raw)
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
