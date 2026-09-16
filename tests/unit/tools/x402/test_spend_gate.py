"""x402 spend gate — turn-origin refusal parity with tx_guard / crypto_trade_gate.

H1a (audit 2026-08-22): x402_fetch accepted `execution_context` and never read it,
so a self-wake / delegation-result / leaf / autonomous-goal turn could auto-sign an
EIP-3009 payment to an attacker-named payTo. Every OTHER money verb refuses that
origin.
"""
import types

import pytest

from tools.x402.spend_gate import x402_spend_refusal


@pytest.fixture(autouse=True)
def bound_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")


def _ctx(**kw):
    """An execution context shaped like the real one (see
    agents/task/agent/core/step_execution.py::_build_execution_context)."""
    base = {"is_sub_agent": False, "role": "orchestrator", "metadata": {},
            "session_id": "sess-1", "user_id": "owner"}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_none_context_is_allowed():
    """A direct/CLI/programmatic call is not an agent turn — parity with
    crypto_trade_gate.trade_turn_refusal, which returns None there. Without this,
    `polyrob` CLI payments and every existing unit test break."""
    assert x402_spend_refusal(None, None) is None


def test_genuine_owner_turn_is_allowed():
    assert x402_spend_refusal(_ctx(), None) is None


def test_leaf_role_is_refused():
    reason = x402_spend_refusal(_ctx(role="leaf"), None)
    assert reason and "forged" in reason.lower()


def test_sub_agent_is_refused():
    assert x402_spend_refusal(_ctx(is_sub_agent=True), None) is not None


def test_self_wake_turn_kind_is_refused():
    reason = x402_spend_refusal(_ctx(metadata={"turn_kind": "self_wake"}), None)
    assert reason is not None


def test_delegation_result_turn_kind_is_refused():
    assert x402_spend_refusal(
        _ctx(metadata={"turn_kind": "delegation_result"}), None) is not None


def test_unset_role_is_refused_fail_closed():
    """A context that cannot prove it is the orchestrator must not spend."""
    ctx = types.SimpleNamespace(is_sub_agent=False, metadata={}, session_id="s")
    assert x402_spend_refusal(ctx, None) is not None


def test_detector_that_raises_refuses(monkeypatch):
    """Fail CLOSED: an import/probe failure must never open a money path."""
    import tools.x402.spend_gate as sg

    def _boom(*_a, **_k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(sg, "_forged_fn", _boom)
    reason = x402_spend_refusal(_ctx(), None)
    assert reason and "could not prove" in reason.lower()
