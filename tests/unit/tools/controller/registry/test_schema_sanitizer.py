"""UP-10 2.5: schema sanitizer unit + per-provider-format round-trip tests."""
import copy

from tools.controller.registry.schema_sanitizer import (
    sanitize_emitted_tools,
    strip_nullable_unions,
    _sanitize_node,
    _sanitize_params_schema,
)


# --- node-level primitives ---------------------------------------------------

def test_nullable_anyof_union_collapses_to_non_null():
    params = {
        "type": "object",
        "properties": {
            "name": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        },
    }
    out = _sanitize_params_schema(params)
    name = out["properties"]["name"]
    assert "anyOf" not in name
    assert name["type"] == "string"
    assert name.get("nullable") is True


def test_ref_sibling_default_stripped():
    schema = {"$ref": "#/$defs/Foo", "default": None}
    out = strip_nullable_unions(schema)  # no-op for $ref union
    out = _sanitize_params_schema({"type": "object", "properties": {"f": schema}})
    f = out["properties"]["f"]
    assert "$ref" in f
    assert "default" not in f


def test_bare_string_object_becomes_object_schema():
    out = _sanitize_node("object", path="t")
    assert out == {"type": "object", "properties": {}}


def test_type_array_with_null_normalized():
    out = _sanitize_node({"type": ["string", "null"]}, path="t")
    assert out["type"] == "string"
    assert out.get("nullable") is True


def test_required_pruned_to_existing_properties():
    params = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a", "ghost"],
    }
    out = _sanitize_params_schema(params)
    assert out["required"] == ["a"]


def test_top_level_combinator_stripped():
    params = {"type": "object", "properties": {}, "oneOf": [{"required": ["x"]}]}
    out = _sanitize_params_schema(params)
    assert "oneOf" not in out


def test_object_missing_properties_gets_empty_dict():
    out = _sanitize_params_schema({"type": "object"})
    assert out["properties"] == {}


# --- per-provider-format round-trips ----------------------------------------

_NULLABLE_PARAMS = {
    "type": "object",
    "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}},
}


def test_openai_format_roundtrip():
    tools = [{"type": "function", "function": {"name": "f", "description": "d",
              "parameters": copy.deepcopy(_NULLABLE_PARAMS)}}]
    out = sanitize_emitted_tools(tools, "openai")
    q = out[0]["function"]["parameters"]["properties"]["q"]
    assert "anyOf" not in q and q["type"] == "string"
    # original untouched (deep copy)
    assert "anyOf" in tools[0]["function"]["parameters"]["properties"]["q"]


def test_anthropic_format_roundtrip_no_null_branch():
    tools = [{"name": "f", "description": "d", "input_schema": copy.deepcopy(_NULLABLE_PARAMS)}]
    out = sanitize_emitted_tools(tools, "anthropic")
    q = out[0]["input_schema"]["properties"]["q"]
    assert "anyOf" not in q
    assert q["type"] == "string"


def test_gemini_format_roundtrip():
    tools = [{"function_declarations": [
        {"name": "f", "description": "d", "parameters": copy.deepcopy(_NULLABLE_PARAMS)}
    ]}]
    out = sanitize_emitted_tools(tools, "gemini")
    q = out[0]["function_declarations"][0]["parameters"]["properties"]["q"]
    assert "anyOf" not in q and q["type"] == "string"


def test_json_fallback_format_roundtrip():
    tools = {
        "type": "object",
        "properties": {
            "action": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "do_thing": {"type": "object", "properties": {
                            "q": {"anyOf": [{"type": "string"}, {"type": "null"}]}}},
                    },
                },
            }
        },
        "required": ["action"],
    }
    out = sanitize_emitted_tools(tools, "default")
    q = out["properties"]["action"]["items"]["properties"]["do_thing"]["properties"]["q"]
    assert "anyOf" not in q and q["type"] == "string"


def test_none_and_non_dict_passthrough():
    assert sanitize_emitted_tools(None) is None
    assert sanitize_emitted_tools([42, "x"]) == [42, "x"]


def test_preserves_additional_properties_false():
    tools = [{"type": "function", "function": {"name": "f", "description": "d",
              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
    out = sanitize_emitted_tools(tools, "openai")
    assert out[0]["function"]["parameters"]["additionalProperties"] is False


def test_default_object_value_is_not_recursed_as_schema():
    """B17: a dict/list default/const/example carries DATA, not a sub-schema. It must
    pass through unchanged — recursing rewrites its string leaves as schema objects."""
    node = {
        "type": "object",
        "properties": {
            "opts": {
                "type": "object",
                "default": {"mode": "path", "items": ["a", "b"]},
                "const": {"fixed": "value"},
                "example": ["x", "y"],
            },
        },
    }
    out = _sanitize_node(node, path="t")
    opts = out["properties"]["opts"]
    assert opts["default"] == {"mode": "path", "items": ["a", "b"]}
    assert opts["const"] == {"fixed": "value"}
    assert opts["example"] == ["x", "y"]
