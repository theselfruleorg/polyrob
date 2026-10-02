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
