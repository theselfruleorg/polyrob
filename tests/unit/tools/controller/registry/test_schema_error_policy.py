"""WS-B1 / B4 — schema-error policy + provider-detection fallback warning.

B1: an invalid native tool schema must NOT be silently shipped to the LLM.
    Default policy (DROP_TOOL) excludes the offending tool from the emitted list;
    RAISE propagates; WARN keeps legacy include-anyway behavior.
B4: get_schema_generator() must warn (once) when it falls back to the default
    generator for an unrecognized provider.
"""
import logging

import pytest
from pydantic import BaseModel, Field

from tools.controller.registry.views import RegisteredAction
from tools.controller.registry import schema_generators as sg
from tools.controller.registry.schema_generators import (
    OpenAISchemaGenerator,
    SchemaValidationError,
    get_schema_generator,
)


class _Params(BaseModel):
    x: str = Field(description="an x")


def _action(name="good_tool", description="does a good thing") -> RegisteredAction:
    return RegisteredAction(
        name=name,
        description=description,
        function=lambda **k: None,
        param_model=_Params,
        tool="t",
    )


# --- B1: schema error policy -------------------------------------------------

def test_valid_tool_is_included():
    gen = OpenAISchemaGenerator()
    tools = gen.generate_tools_list([_action()])
    assert [t["function"]["name"] for t in tools] == ["good_tool"]


def test_drop_tool_policy_excludes_invalid_tool(monkeypatch):
    # Empty description -> OpenAI schema validation flags missing description -> invalid
    monkeypatch.setenv("TOOL_SCHEMA_ERROR_POLICY", "DROP_TOOL")
    gen = OpenAISchemaGenerator()
    tools = gen.generate_tools_list([_action(), _action(name="bad", description="")])
    names = [t["function"]["name"] for t in tools]
    assert "good_tool" in names
    assert "bad" not in names  # dropped, not shipped


def test_default_policy_is_drop_tool(monkeypatch):
    monkeypatch.delenv("TOOL_SCHEMA_ERROR_POLICY", raising=False)
    gen = OpenAISchemaGenerator()
    tools = gen.generate_tools_list([_action(name="bad", description="")])
    assert tools == []  # invalid tool dropped by default


def test_raise_policy_propagates(monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMA_ERROR_POLICY", "RAISE")
    gen = OpenAISchemaGenerator()
    with pytest.raises(SchemaValidationError):
        gen.generate_tools_list([_action(name="bad", description="")])


def test_warn_policy_keeps_invalid_tool(monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMA_ERROR_POLICY", "WARN")
    gen = OpenAISchemaGenerator()
    tools = gen.generate_tools_list([_action(name="bad", description="")])
    assert [t["function"]["name"] for t in tools] == ["bad"]  # legacy: shipped anyway


# --- B4: provider fallback warning -------------------------------------------

def test_known_provider_no_warning(caplog):
    sg._warned_fallback_providers.clear()
    with caplog.at_level(logging.WARNING):
        get_schema_generator("openai-gpt4")
    assert not any("fallback" in r.message.lower() for r in caplog.records)


def test_unknown_provider_warns_once(caplog):
    sg._warned_fallback_providers.clear()
    with caplog.at_level(logging.WARNING):
        get_schema_generator("totally-unknown-llm")
        get_schema_generator("totally-unknown-llm")  # second call must not warn again
    warnings = [r for r in caplog.records if "totally-unknown-llm" in r.message]
    assert len(warnings) == 1
