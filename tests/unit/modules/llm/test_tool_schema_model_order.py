"""GLM writes its arguments in alphabetical order, and an upstream that decodes with the
schema's order fixed cannot go back to a property GLM writes later. Prod 2026-11-23:
``room_read`` came without ``room`` five times and the run ended as a loop, and
``skill_manage`` patches came without ``old_string``."""
import copy

import pytest

from modules.llm.tool_schemas import tools_in_model_order

ROOM = [{"type": "function", "function": {"name": "room_read", "parameters": {
    "type": "object",
    "properties": {"room": {"type": "string"}, "limit": {"type": "integer"},
                   "filter": {"type": "object",
                              "properties": {"z": {"type": "string"}, "a": {"type": "string"}}}},
    "required": ["room"]}}}]


def _props(tools):
    return tools[0]["function"]["parameters"]["properties"]


@pytest.mark.parametrize("model", ["z-ai/glm-5.3", "glm-5.3", "z-ai/glm-5.3-flash", "zai-org/GLM-5.2"])
def test_glm_gets_sorted_properties_at_every_depth(model):
    before = copy.deepcopy(ROOM)
    out = tools_in_model_order(ROOM, model)
    assert list(_props(out)) == ["filter", "limit", "room"]
    assert list(_props(out)["filter"]["properties"]) == ["a", "z"]
    assert out == before                       # the same schema, only the order differs
    assert list(_props(ROOM)) == list(_props(before))   # the shared cached copy is untouched


@pytest.mark.parametrize("model", ["deepseek/deepseek-v4.1-flash", "anthropic/claude-haiku-5.5", None])
def test_other_families_keep_the_same_object(model):
    assert tools_in_model_order(ROOM, model) is ROOM


def test_a_property_named_properties_is_a_name_not_a_schema_map():
    tools = [{"function": {"name": "t", "parameters": {"type": "object", "properties": {
        "properties": {"type": "string", "description": "d"}, "a": {"type": "string"}}}}}]
    out = tools_in_model_order(tools, "glm-5.3")
    assert out == tools and list(_props(out)) == ["a", "properties"]


def test_the_openai_shaped_request_carries_the_order():
    import logging

    from modules.llm.openai_client import OpenAIClient
    c = object.__new__(OpenAIClient)
    c.logger, c.model_type = logging.getLogger("t"), "glm-5.3"
    c.max_tokens, c.temperature = 1024, 0.0
    c._adjust_max_tokens = lambda **kw: 1024
    c._format_messages = lambda m: m
    c._stable_prompt_cache_key = lambda s: None
    p = c._build_tool_request_params([{"role": "user", "content": "x"}], ROOM, None, None, None, {})
    assert list(_props(p["tools"])) == ["filter", "limit", "room"]
