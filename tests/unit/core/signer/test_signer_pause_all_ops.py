"""The signer-owned pause covers every operation that can authorize value."""
import pytest
from core.signer import protocol
from tests.unit.core.signer.test_066_p2_signer_schemas import _x402

@pytest.mark.parametrize("op", ["evm.send", "evm.verdict", "x402.authorize",
                                "deposit.sweep", "venue.sign", "eip8004.feedback_auth"])
def test_paused_signer_never_dispatches_signing_operation(rig, monkeypatch, op):
    assert rig.call("pause.set", {"paused": True}, uid=0)["ok"]
    def forbidden(*args):
        pytest.fail("paused signing handler ran")
    monkeypatch.setattr(rig.service, "_op_" + op.replace(".", "_"), forbidden)
    assert rig.call(op, {})["code"] == protocol.PAUSED
    assert rig.call("ping")["result"]["paused"] is True
    assert rig.call("identity")["ok"]


def test_x402_signing_resumes_after_owner_lifts_pause(rig):
    body = _x402(rig)
    assert rig.call("pause.set", {"paused": True}, uid=0)["ok"]
    assert rig.call("x402.authorize", body)["code"] == protocol.PAUSED
    assert rig.call("pause.set", {"paused": False}, uid=0)["ok"]
    assert rig.call("x402.authorize", body)["ok"]
