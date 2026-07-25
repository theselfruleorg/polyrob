"""T1.2: reason-specific owner-facing phrases layered on the outage-notice rail.

Hermes never relays raw model/provider text to the user; this module maps the
core.error_classifier.FailoverReason taxonomy (the SSOT the step loop and the
LLM-outage notice already share) onto short, actionable sentences. Truth-table
coverage: each outage-class reason -> its exact phrase; every other reason ->
None; and a proof that raw input text never leaks into the returned phrase.
"""
import pytest

from core.error_classifier import FailoverReason
from core.surfaces.error_notices import notice_for, notice_for_texts

CREDIT_DEATH_PHRASE = (
    "The model provider reports the account is out of credits — top up or "
    "switch provider, then send another message."
)
AUTH_PERMANENT_PHRASE = (
    "The provider API key was rejected (invalid or deactivated) — fix the "
    "key configuration, then send another message."
)
PROVIDER_EXHAUSTED_PHRASE = (
    "All configured model providers failed — check provider status/keys, "
    "then send another message."
)

_TRUTH_TABLE = [
    (FailoverReason.CREDIT_DEATH, CREDIT_DEATH_PHRASE),
    (FailoverReason.AUTH_PERMANENT, AUTH_PERMANENT_PHRASE),
    (FailoverReason.PROVIDER_EXHAUSTED, PROVIDER_EXHAUSTED_PHRASE),
    (FailoverReason.CONTEXT_OVERFLOW, None),
    (FailoverReason.RATE_LIMIT, None),
    (FailoverReason.CONNECTION, None),
    (FailoverReason.VALIDATION, None),
    (FailoverReason.GENERIC_LLM, None),
    (FailoverReason.UNKNOWN, None),
]


@pytest.mark.parametrize("reason,expected", _TRUTH_TABLE)
def test_notice_for_truth_table(reason, expected):
    assert notice_for(reason) == expected


def test_notice_for_texts_credit_death_real_402_shape():
    text = "OpenRouter generation failed: Error code: 402 - This request requires more credits."
    assert notice_for_texts(text) == CREDIT_DEATH_PHRASE


def test_notice_for_texts_non_outage_status_is_none():
    assert notice_for_texts("done: wrote report.md") is None


def test_notice_for_texts_no_texts_is_none():
    assert notice_for_texts() is None
    assert notice_for_texts(None, "", None) is None


def test_notice_for_texts_returns_first_outage_reason_in_order():
    # First text is non-outage (UNKNOWN), second is CREDIT_DEATH -> phrase found
    # on the second text, matching looks_like_llm_outage's iteration order.
    assert notice_for_texts("Session failed: browser crashed",
                             "Error code: 402 - insufficient credits") == CREDIT_DEATH_PHRASE
    # First text already resolves -> short-circuits, never inspects the second.
    assert notice_for_texts("Error code: 402 - insufficient credits",
                             "invalid_api_key rejected") == CREDIT_DEATH_PHRASE


def test_no_phrase_leaks_raw_provider_or_model_text():
    """The returned phrase is always one of the three fixed sentences — never
    an f-string built from the classified input text."""
    raw_texts = [
        "Error code: 402 - This request requires more credits from OpenRouter, model gpt-secret-x",
        "invalid_api_key: sk-proj-abcdef1234567890 rejected by Anthropic",
        "All LLM providers exhausted: ['openrouter', 'anthropic', 'deepseek-super-secret-model']",
    ]
    all_phrases = {CREDIT_DEATH_PHRASE, AUTH_PERMANENT_PHRASE, PROVIDER_EXHAUSTED_PHRASE}
    for text in raw_texts:
        phrase = notice_for_texts(text)
        assert phrase in all_phrases
        # Every substring unique to the raw text must not appear in the phrase.
        for token in ("gpt-secret-x", "sk-proj-abcdef1234567890", "deepseek-super-secret-model",
                      "OpenRouter", "Anthropic", "openrouter", "anthropic"):
            assert token not in phrase
