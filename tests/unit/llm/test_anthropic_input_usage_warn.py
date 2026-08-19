"""P8a (context-usage audit 2026-08-15) — input-side usage blindness must be LOUD.

Live artifact (zai-coding/glm-5, 2026-08-14): llm_usage recorded
prompt_tokens=0 / cached_tokens=0 while completion_tokens=295 — the endpoint
returned output usage but no input usage, and the meter silently recorded 0.
_extract_usage_data now emits a one-time WARN naming the provider and the raw
usage payload so the blindness is diagnosable instead of silent.
"""
import logging
from types import SimpleNamespace

from modules.llm.anthropic_client import AnthropicClient


def _client_with_usage(input_tokens, output_tokens):
    c = object.__new__(AnthropicClient)
    c.logger = logging.getLogger("test.usage_warn")
    c.model_type = "glm-5"
    c._PROVIDER_LABEL = "z.ai GLM Coding Plan"
    usage = SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_input_tokens=0, cache_creation_input_tokens=0)
    c.last_response = SimpleNamespace(usage=usage)
    return c


def test_output_without_input_warns_once(caplog):
    c = _client_with_usage(input_tokens=None, output_tokens=295)
    with caplog.at_level(logging.WARNING, logger="test.usage_warn"):
        c._extract_usage_data()
        c._extract_usage_data()
    warns = [r for r in caplog.records
             if "input-side usage missing" in r.getMessage()]
    assert len(warns) == 1  # one-time, not per call
    assert "z.ai GLM Coding Plan" in warns[0].getMessage()


def test_zero_input_with_output_also_warns(caplog):
    c = _client_with_usage(input_tokens=0, output_tokens=295)
    with caplog.at_level(logging.WARNING, logger="test.usage_warn"):
        usage = c._extract_usage_data()
    assert any("input-side usage missing" in r.getMessage()
               for r in caplog.records)
    # Extraction stays honest: 0 in -> 0 recorded (not fabricated).
    assert usage['prompt_tokens'] == 0
    assert usage['completion_tokens'] == 295


def test_healthy_usage_does_not_warn(caplog):
    c = _client_with_usage(input_tokens=1200, output_tokens=295)
    with caplog.at_level(logging.WARNING, logger="test.usage_warn"):
        usage = c._extract_usage_data()
    assert not any("input-side usage missing" in r.getMessage()
                   for r in caplog.records)
    assert usage['prompt_tokens'] == 1200
