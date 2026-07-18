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
