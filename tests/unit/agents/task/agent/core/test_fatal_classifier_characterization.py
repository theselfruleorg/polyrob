"""Credit death deeper in the exception chain reaches the fatal check."""
from core.exceptions import LLMError, LLMProviderExhaustedError

def test_chain_wrapped_billing_now_fatal_when_failover_off():
    inner = LLMError("error code: 402 - requires more credits")
    outer = LLMProviderExhaustedError("No fallback available after LLMPermanentError")
    outer.__context__ = inner
    # Top-string of `outer` has no billing text; chain walk finds the inner 402.
    from agents.task.agent.core.step import _is_fatal_step_exc
    assert _is_fatal_step_exc(outer, billing_failover_enabled=False) is True
    assert _is_fatal_step_exc(outer, billing_failover_enabled=True) is False
