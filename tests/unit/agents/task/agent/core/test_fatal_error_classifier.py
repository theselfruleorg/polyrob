"""The step loop halts on the error's type: a credential or a configuration error at once,
credit death by the billing failover flag. A provider's text reaches a type in
``translate_llm_error``, so the real provider shapes run through it here."""
import pytest

from agents.task.agent.core.step import _is_fatal_step_exc
from core.exceptions import LLMConfigError
from modules.llm.llm_client import translate_llm_error

_REAL_OPENROUTER_402 = (
    '402 "This request requires more credits, or fewer max_tokens. '
    'visit https://openrouter.ai/settings/credits and add more credits"'
)


def _typed(text):
    return translate_llm_error(Exception(text))


@pytest.mark.parametrize("failover", [True, False])
@pytest.mark.parametrize("text", ["authentication failed", "invalid api key"])
def test_auth_always_fatal(text, failover):
    assert _is_fatal_step_exc(_typed(text), billing_failover_enabled=failover) is True


@pytest.mark.parametrize("failover", [True, False])
def test_a_bad_configuration_is_fatal(failover):
    assert _is_fatal_step_exc(LLMConfigError("no key"), billing_failover_enabled=failover) is True


@pytest.mark.parametrize("failover", [True, False])
@pytest.mark.parametrize("text", [
    "error 429 too many requests", "rate limit exceeded", "monthly quota exceeded",
    "connection reset by peer", "something totally unknown",
])
def test_transient_or_unknown_is_not_fatal(text, failover):
    assert _is_fatal_step_exc(_typed(text), billing_failover_enabled=failover) is False


@pytest.mark.parametrize("text", [
    "insufficient_quota", "billing hard limit reached", "error 402 billing", _REAL_OPENROUTER_402,
])
def test_credit_death_follows_the_failover_flag(text):
    assert _is_fatal_step_exc(_typed(text), billing_failover_enabled=False) is True
    assert _is_fatal_step_exc(_typed(text), billing_failover_enabled=True) is False
