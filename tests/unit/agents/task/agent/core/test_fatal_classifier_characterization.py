"""Characterize _is_fatal_step_error before/after routing through the classifier."""
from agents.task.agent.core.step import _is_fatal_step_error
from core.exceptions import LLMError, LLMProviderExhaustedError

# (text, billing_failover_enabled, expected_fatal) — the CURRENT truth table.
_CASES = [
    ("quota exceeded for this org", True, True),
    ("authentication failed", True, True),
    ("invalid api key", True, True),
    ("error code: 402 - requires more credits", True, False),   # billing + failover ON → not fatal
    ("error code: 402 - requires more credits", False, True),    # billing + failover OFF → fatal
    ("429 rate limit reached", True, False),
    ("connection reset by peer", True, False),
    ("something totally unknown", True, False),
]


def test_is_fatal_step_error_truth_table_preserved():
    for text, failover, expected in _CASES:
        assert _is_fatal_step_error(text.lower(), failover) is expected, text


def test_chain_wrapped_billing_now_fatal_when_failover_off():
    # New overload: pass the exception so classification can walk the cause chain.
    inner = LLMError("error code: 402 - requires more credits")
    outer = LLMProviderExhaustedError("No fallback available after LLMPermanentError")
    outer.__context__ = inner
    # Top-string of `outer` has no billing text; chain walk finds the inner 402.
    from agents.task.agent.core.step import _is_fatal_step_exc
    assert _is_fatal_step_exc(outer, billing_failover_enabled=False) is True
    assert _is_fatal_step_exc(outer, billing_failover_enabled=True) is False
