"""The Solana broadcast rail. Phase 3.

Mirrors `EvmRail`'s job — build, size, sign, send, confirm — against a different
transaction model. What replaces `chainId` as the replay bound is the recent
BLOCKHASH: a Solana transaction is valid only for ~150 slots (about a minute),
so a stale one cannot be replayed later, but it also silently expires.
"""
import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from core.wallet.solana_rail import SolanaRail, SolanaBroadcastError


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
        _rail(rpc=_rpc).send_raw(b"\x01\x02")


def test_a_successful_send_returns_the_signature():
    rail = _rail(rpc=lambda m, p: "5xSig" if m == "sendTransaction" else {})
    assert rail.send_raw(b"\x01\x02") == "5xSig"


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
