"""_classify_llm_error substring bug (027): `"rate" in s` matched the word
'geneRATE', so EVERY provider error phrased 'Failed to generate response: …'
— including a permanent 401 — was classified rate_limit and retried with
backoff (live clean-room: a bad key burned 4 attempts before halting)."""

from agents.task.agent.core.next_action_internal import NextActionInternalMixin


classify = NextActionInternalMixin._classify_llm_error


def test_permanent_401_is_not_classified_rate_limit():
    err = Exception(
        "from OpenRouter: Failed to generate response: OpenRouter: "
        "Error code: 401 - {'error': {'message': 'User not found.', 'code': 401}}"
    )
    assert classify(err) == "auth"


def test_generate_wording_alone_is_not_a_rate_limit():
    assert classify(Exception("Failed to generate response: boom")) != "rate_limit"


def test_real_rate_limit_still_classified():
    assert classify(Exception("429 Too Many Requests: rate limit exceeded")) == "rate_limit"


def test_parse_errors_still_classified():
    assert classify(Exception("json parse failure in tool_call")) == "parse"
