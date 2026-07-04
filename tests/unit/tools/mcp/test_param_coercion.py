"""Tests for tools.mcp.param_coercion — the extracted JSON-schema coercion engine.

TDD: these tests are written BEFORE the module exists (RED), then pass after extraction (GREEN).
"""
import pytest
from tools.mcp.param_coercion import coerce_arguments, enhance_schema_with_date_hints


# ---------------------------------------------------------------------------
# coerce_arguments — the main entry point
# ---------------------------------------------------------------------------

class TestCoerceArguments:
    """Tests for coerce_arguments(schema, arguments, tool_name, *, logger=None)."""

    def test_integer_string_coerced_to_int(self):
        schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
        converted, errors = coerce_arguments(schema, {"count": "5"}, "test_tool")
        assert errors == []
        assert converted == {"count": 5}

    def test_integer_already_int_passes_through(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        converted, errors = coerce_arguments(schema, {"n": 42}, "tool")
        assert errors == []
        assert converted["n"] == 42

    def test_integer_float_coerced(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        converted, errors = coerce_arguments(schema, {"x": 3.9}, "tool")
        assert errors == []
        assert converted["x"] == 3

    def test_integer_non_convertible_string_gives_error(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        converted, errors = coerce_arguments(schema, {"n": "notanumber"}, "tool")
        assert len(errors) == 1
        assert "notanumber" in errors[0]
        assert "n" in errors[0]

    def test_missing_required_param_gives_error(self):
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        converted, errors = coerce_arguments(schema, {}, "search_tool")
        assert any("q" in e for e in errors)

    def test_string_coerced_from_int(self):
        schema = {"type": "object", "properties": {"label": {"type": "string"}}}
        converted, errors = coerce_arguments(schema, {"label": 42}, "tool")
        assert errors == []
        assert converted["label"] == "42"

    def test_boolean_passes_through(self):
        schema = {"type": "object", "properties": {"flag": {"type": "boolean"}}}
        converted, errors = coerce_arguments(schema, {"flag": True}, "tool")
        assert errors == []
        assert converted["flag"] is True

    def test_dict_object_passes_through(self):
        schema = {"type": "object", "properties": {"meta": {"type": "object"}}}
        payload = {"key": "val"}
        converted, errors = coerce_arguments(schema, {"meta": payload}, "tool")
        assert errors == []
        assert converted["meta"] == payload

    def test_object_wrong_type_gives_error(self):
        schema = {"type": "object", "properties": {"meta": {"type": "object"}}}
        converted, errors = coerce_arguments(schema, {"meta": "not-a-dict"}, "tool")
        assert len(errors) == 1
        assert "meta" in errors[0]

    def test_array_list_passes_through(self):
        schema = {"type": "object", "properties": {"ids": {"type": "array"}}}
        converted, errors = coerce_arguments(schema, {"ids": [1, 2, 3]}, "tool")
        assert errors == []
        assert converted["ids"] == [1, 2, 3]

    def test_array_wrong_type_gives_error(self):
        schema = {"type": "object", "properties": {"ids": {"type": "array"}}}
        converted, errors = coerce_arguments(schema, {"ids": "not-a-list"}, "tool")
        assert len(errors) == 1
        assert "ids" in errors[0]

    def test_extra_param_not_in_schema_passes_through(self):
        """Unknown params are allowed (some tools accept them)."""
        schema = {"type": "object", "properties": {}}
        converted, errors = coerce_arguments(schema, {"extra": "val"}, "tool")
        assert errors == []
        assert converted["extra"] == "val"

    def test_integer_minimum_constraint_enforced(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 10}},
        }
        converted, errors = coerce_arguments(schema, {"n": 5}, "tool")
        assert len(errors) == 1
        assert "minimum" in errors[0]

    def test_integer_maximum_constraint_enforced(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer", "maximum": 100}},
        }
        converted, errors = coerce_arguments(schema, {"n": 200}, "tool")
        assert len(errors) == 1
        assert "maximum" in errors[0]

    def test_all_correct_dict_unchanged(self):
        """Already-valid parameters produce empty errors and correct conversion."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["name"],
        }
        converted, errors = coerce_arguments(schema, {"name": "alice", "count": 7}, "tool")
        assert errors == []
        assert converted == {"name": "alice", "count": 7}

    def test_accepts_optional_logger_param(self):
        """coerce_arguments must accept an optional logger kwarg without error."""
        import logging
        logger = logging.getLogger("test")
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        converted, errors = coerce_arguments(schema, {"x": "3"}, "tool", logger=logger)
        assert errors == []
        assert converted["x"] == 3


# ---------------------------------------------------------------------------
# enhance_schema_with_date_hints — exposed helper
# ---------------------------------------------------------------------------

class TestEnhanceSchemaWithDateHints:
    """Tests for enhance_schema_with_date_hints(schema) — pure schema enrichment."""

    def test_date_integer_param_gets_hint(self):
        schema = {
            "type": "object",
            "properties": {"start_date": {"type": "integer"}},
        }
        enhanced = enhance_schema_with_date_hints(schema)
        desc = enhanced["properties"]["start_date"].get("description", "")
        assert "auto-convert" in desc.lower() or "date" in desc.lower()

    def test_non_date_integer_param_unchanged(self):
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
        }
        enhanced = enhance_schema_with_date_hints(schema)
        # count is not a date keyword → no date hint added
        assert "auto-convert" not in enhanced["properties"]["count"].get("description", "")

    def test_original_schema_not_mutated(self):
        schema = {
            "type": "object",
            "properties": {"from_date": {"type": "integer"}},
        }
        original_props = dict(schema["properties"])
        enhance_schema_with_date_hints(schema)
        # Original untouched
        assert schema["properties"] == original_props


# --- Regression: defensive guards + value-based boolean/string coercion ---

def test_boolean_string_false_is_false():
    schema = {"properties": {"flag": {"type": "boolean"}}}
    out, errors = coerce_arguments(schema, {"flag": "false"}, "t")
    assert errors == []
    assert out["flag"] is False  # was True under bool("false")


def test_boolean_string_true_variants():
    schema = {"properties": {"flag": {"type": "boolean"}}}
    for v in ("true", "1", "yes", "on", "TRUE"):
        out, errors = coerce_arguments(schema, {"flag": v}, "t")
        assert errors == [] and out["flag"] is True, v
    for v in ("false", "0", "no", "off", ""):
        out, errors = coerce_arguments(schema, {"flag": v}, "t")
        assert errors == [] and out["flag"] is False, v


def test_boolean_unparseable_is_error():
    schema = {"properties": {"flag": {"type": "boolean"}}}
    out, errors = coerce_arguments(schema, {"flag": "maybe"}, "t")
    assert errors and "boolean" in errors[0].lower()


def test_string_param_dict_is_error_not_garbage():
    schema = {"properties": {"name": {"type": "string"}}}
    out, errors = coerce_arguments(schema, {"name": {"a": 1}}, "t")
    assert errors and "string" in errors[0].lower()
    assert "name" not in out  # never produces "{'a': 1}"


def test_non_dict_arguments_returns_error_not_crash():
    schema = {"properties": {}, "required": ["x"]}
    out, errors = coerce_arguments(schema, None, "t")  # was AttributeError
    assert out == {} and errors


def test_non_list_required_does_not_crash():
    schema = {"properties": {"x": {"type": "string"}}, "required": "x"}  # malformed
    out, errors = coerce_arguments(schema, {"x": "ok"}, "t")  # was TypeError
    assert out["x"] == "ok"
