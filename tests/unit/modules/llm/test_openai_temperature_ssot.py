"""H1: the temperature-strip check was a hardcoded substring list
['o1','o1-mini','o3-mini','o1-preview'] duplicated in 4 places. The registry ships
o3 and o4-mini, which are NOT substrings of any entry, so temperature=0.0 was sent and
OpenAI 400'd both models. Derive the decision from one SSOT prefix-based helper.
"""
from modules.llm.model_registry import openai_reasoning_model, openai_omits_temperature


def test_o3_and_o4_mini_are_reasoning_models():
    assert openai_reasoning_model("o3")
    assert openai_reasoning_model("o4-mini")


def test_o1_family_reasoning():
    assert openai_reasoning_model("o1")
    assert openai_reasoning_model("o1-mini")
    assert openai_reasoning_model("o1-preview")


def test_gpt4o_is_not_a_reasoning_model():
    assert not openai_reasoning_model("gpt-4o")
    assert not openai_reasoning_model("gpt-4.1")


def test_omit_temperature_covers_o_series_and_gpt5():
    assert openai_omits_temperature("o3")
    assert openai_omits_temperature("o4-mini")
    assert openai_omits_temperature("gpt-5")
    assert openai_omits_temperature("gpt-5-mini")


def test_gpt4o_keeps_temperature():
    assert not openai_omits_temperature("gpt-4o")


def test_provider_prefixed_names_resolved():
    assert openai_reasoning_model("openai/o3")
    assert openai_omits_temperature("openai/gpt-5")
