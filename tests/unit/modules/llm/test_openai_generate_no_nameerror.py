"""Regression (P0): OpenAIClient._generate referenced two names that were never
defined — is_reasoning_model / is_temp_restricted (lines 224/236) — a leftover from
the refactor that migrated the sibling temperature checks to the SSOT helper
openai_omits_temperature() but missed these two. Every non-tool-calling
generate_response() (aux / compaction / document-processing) raised NameError,
caught and re-raised as ServiceError.

Guard the exact regression: the dead names must not appear as free/global lookups
in _generate's code object, and the SSOT helper must be the temperature decision.
"""
from modules.llm.openai_client import OpenAIClient


def test_generate_has_no_undefined_temperature_names():
    names = OpenAIClient._generate.__code__.co_names
    assert "is_reasoning_model" not in names, "undefined name reintroduced → NameError"
    assert "is_temp_restricted" not in names, "undefined name reintroduced → NameError"


def test_generate_uses_ssot_temperature_helper():
    # openai_omits_temperature is imported locally inside _generate; its call
    # shows up either as a local var or a global lookup depending on binding.
    src = OpenAIClient._generate.__code__
    consts_and_names = set(src.co_names) | set(src.co_varnames)
    assert "openai_omits_temperature" in consts_and_names
