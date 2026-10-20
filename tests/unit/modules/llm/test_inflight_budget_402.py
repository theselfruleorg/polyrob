"""OpenRouter's in-flight 402 is a back-off, not a dead account (prod 2026-10-12).

OpenRouter answers ``402 ... 'This request would exceed your available credits
given your current in-flight requests. Retry after in-flight requests settle'``
with ``metadata.reason = in_flight_budget_exceeded`` when the SUM of concurrent
reservations passes the balance. The account is funded; the request succeeds
once a sibling request settles. Reading it as credit death latched the
sentinel (autonomy paused, owner pinged "resume in 6h"), skipped the funded
seat, and failed a goal over to a dead provider.
"""
from core.credit_sentinel import looks_like_credit_death, looks_like_transient_budget
from core.error_classifier import FailoverReason, classify_error
from core.exceptions import LLMPermanentError, LLMRateLimitError
from modules.llm.credit_errors import provider_credit_error
from modules.llm.llm_client import translate_llm_error

PROD_TEXT = (
    "OpenRouter generation failed: Error code: 402 - {'error': {'message': "
    "'This request would exceed your available credits given your current "
    "in-flight requests. Retry after in-flight requests settle, or add credits.', "
    "'code': 402, 'metadata': {'reason': 'in_flight_budget_exceeded'}}}")


def test_inflight_402_is_transient_not_credit_death():
    assert looks_like_transient_budget(PROD_TEXT) is True
    assert looks_like_credit_death(PROD_TEXT) is False


def test_real_402_is_still_credit_death():
    dead = "Error code: 402 - {'error': {'message': 'Insufficient credits. Add more using https://openrouter.ai/settings/credits'}}"
    assert looks_like_transient_budget(dead) is False
    assert looks_like_credit_death(dead) is True


def test_translate_maps_inflight_402_to_rate_limit():
    err = translate_llm_error(Exception(PROD_TEXT), "from OpenRouter")
    assert isinstance(err, LLMRateLimitError)
    assert not isinstance(err, LLMPermanentError)


def test_classifier_reads_inflight_402_as_rate_limit():
    err = translate_llm_error(Exception(PROD_TEXT), "from OpenRouter")
    assert classify_error(err).reason is FailoverReason.RATE_LIMIT


def test_provider_credit_error_ignores_inflight_402():
    assert provider_credit_error("OpenRouter", Exception(PROD_TEXT)) is None
