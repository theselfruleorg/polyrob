"""P0 Task 1 — ExecutionRequest.network field."""
from tools.code_exec.result import ExecutionRequest


def test_network_defaults_to_none():
    req = ExecutionRequest(language="python", code="print(1)")
    assert req.network is None


def test_network_can_be_set_none_policy():
    req = ExecutionRequest(language="python", code="print(1)", network="none")
    assert req.network == "none"


def test_network_accepts_egress_and_host():
    assert ExecutionRequest(language="bash", code="true", network="egress").network == "egress"
    assert ExecutionRequest(language="bash", code="true", network="host").network == "host"
