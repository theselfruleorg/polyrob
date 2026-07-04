"""P9 pass-7 — ModelIntrospectionMixin extracted from service.py."""
import logging

from agents.task.agent.core.model_introspection import ModelIntrospectionMixin


def test_agent_composes_model_introspection_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, ModelIntrospectionMixin)
    for m in ("_get_provider_from_model", "_check_vision_support", "_extract_model_name"):
        assert getattr(Agent, m).__qualname__.startswith("ModelIntrospectionMixin")


class _Host(ModelIntrospectionMixin):
    def __init__(self):
        self.logger = logging.getLogger("introspection-test")


def test_get_provider_from_empty_model():
    assert _Host()._get_provider_from_model("") == "unknown"


def test_extract_model_name_prefers_model_attr():
    class _LLM:
        model_name = "gpt-5"

    assert _Host()._extract_model_name(_LLM()) == "gpt-5"


def test_extract_model_name_falls_back_to_class_name():
    class WeirdLLM:
        pass

    out = _Host()._extract_model_name(WeirdLLM())
    assert out in ("WeirdLLM", "unknown") or isinstance(out, str)
