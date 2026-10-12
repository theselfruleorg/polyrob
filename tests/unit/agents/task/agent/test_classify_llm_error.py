"""P7 finalization: LLM-error classification extracted from _get_next_action_internal
into a pure, testable helper _classify_llm_error."""
import pytest

from agents.task.agent.core.next_action_internal import NextActionInternalMixin

c = NextActionInternalMixin._classify_llm_error


@pytest.mark.parametrize("msg,expected", [
    ("failed to parse JSON", "parse"),
    ("schema validation error", "parse"),
    ("429 rate limit exceeded", "rate_limit"),
    ("request limit reached", "rate_limit"),
    ("llm_client bad parameter", "parameter_error"),
    ("connection reset by peer", "other"),
])
def test_classification_buckets(msg, expected):
    assert c(Exception(msg)) == expected


ZAI_1113 = ("Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error', "
            "'code': '1113', 'message': '[1113][Insufficient balance or no resource "
            "package. Please recharge.][2026100915015121e7ff0801f04d4c]'}}")


def test_credit_refusal_dressed_as_429_is_billing_not_rate_limit():
    """D3 (prod 2026-10-09): z.ai code 1113 was retried 4x as a rate limit."""
    assert c(Exception(ZAI_1113)) == "billing"


def test_a_permanent_error_is_never_retried_as_rate_limit():
    from core.exceptions import LLMPermanentError
    assert c(LLMPermanentError("from zai-coding: 429 something")) == "billing"
