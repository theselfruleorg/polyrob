"""An unknown broadcast outcome is never reported as "not sent" (release validation, 1.2.0).

The remote signer can receive a request, broadcast it and lose its reply; the
Solana RPC can accept the bytes and lose its reply. Both raise an UNKNOWN
outcome. Every send handler reported it as "funds were NOT sent" / "nothing was
sent", which invites a resend that pays twice.
"""
import pytest

from core.wallet.broadcast.evm import (
    BroadcastError, BroadcastOutcomeUnknown, broadcast_failure_text,
    outcome_unknown)


@pytest.mark.parametrize("exc", [
    BroadcastOutcomeUnknown("polyrob-signer outcome UNKNOWN — the request was sent"),
    RuntimeError("submission outcome unknown for 5sig; reconcile before retrying"),
    RuntimeError("RPC did not confirm the expected signature 5sig; reconcile before retrying"),
])
def test_unknown_outcomes_never_say_not_sent(exc):
    assert outcome_unknown(exc)
    text = broadcast_failure_text(exc, nothing="funds were NOT sent")
    assert "NOT sent" not in text and "nothing was sent" not in text
    assert "outcome unknown" in text and "do not send again" in text


@pytest.mark.parametrize("exc", [
    BroadcastError("polyrob-signer refused: cap [over_cap]"),
    RuntimeError("nonce too low"),
])
def test_a_definite_refusal_still_says_nothing_was_sent(exc):
    assert not outcome_unknown(exc)
    assert broadcast_failure_text(exc) == f"broadcast failed: {exc} — nothing was sent"


def test_the_bridge_leg_parks_an_unknown_send_in_flight():
    from types import SimpleNamespace
    from tools.defi.bridge_evm_leg import EvmOriginLeg

    class _Rail:
        def sign_and_send(self, tx):
            raise BroadcastOutcomeUnknown("polyrob-signer outcome UNKNOWN")

    leg = EvmOriginLeg.send(SimpleNamespace(rail=_Rail(), tx={}))
    assert leg.state == "unknown"
    assert "NOT sent" not in leg.detail and "nothing was sent" not in leg.detail


@pytest.mark.parametrize("reply", [{}, {"tx_hash": ""}, {"tx_hash": "garbage"}, None])
def test_a_remote_signer_reply_without_a_hash_is_unknown_and_holds_the_interlock(
        monkeypatch, tmp_path, reply):
    """The signer received the request and answered nonsense: it may have
    broadcast. A plain error read as "failed" and a retry could pay twice."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from core.signer import attest
    from core.wallet import submission_journal
    from core.wallet.broadcast.evm import EvmRail

    class _Signer:
        address = "0x" + "1" * 40
        remote_send = True

        def send_transaction(self, tx, *, chain, intent):
            return reply if reply is not None else {"tx_hash": None}

    monkeypatch.setattr(attest, "take", lambda tx: object())
    rail = EvmRail("base", _Signer())
    rail.preflight = lambda: (True, "")
    tx = {"chainId": rail.chain_id, "nonce": 1, "to": "0x" + "2" * 40, "value": 1}
    with pytest.raises(BroadcastOutcomeUnknown) as info:
        rail._remote_sign_and_send(tx)
    assert outcome_unknown(info.value)
    rows = submission_journal.unresolved()
    assert len(rows) == 1 and rows[0]["tx_hash"].startswith("attempt:")
