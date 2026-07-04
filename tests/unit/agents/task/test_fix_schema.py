"""TDD tests for fix_schema_for_provider and its backward-compat wrappers.

RED phase: these tests fail until fix_schema_for_provider is created.
GREEN phase: passes after implementation.
"""

import copy
import pytest
from agents.task.utils import fix_openai_schema, fix_anthropic_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_nested_object():
    return {
        "type": "object",
        "properties": {
            "config": {
                "type": "object",
                "properties": {
                    "timeout": {"type": "integer"},
                },
            }
        },
    }


def _base_array_of_objects():
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
        },
    }


def _already_correct():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "x": {"type": "string"},
        },
    }


def _explicit_true():
    """Dict[str, Any] pattern — additionalProperties must be preserved as True."""
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {},
    }


# ---------------------------------------------------------------------------
# Tests for fix_schema_for_provider
# ---------------------------------------------------------------------------

class TestFixSchemaForProvider:
    def test_import_exists(self):
        from agents.task.utils import fix_schema_for_provider  # noqa: F401

    def test_openai_nested_object_gets_additional_false(self):
        from agents.task.utils import fix_schema_for_provider
        schema = _base_nested_object()
        result = fix_schema_for_provider(copy.deepcopy(schema), "openai")
        assert result["additionalProperties"] is False
        assert result["properties"]["config"]["additionalProperties"] is False

    def test_anthropic_nested_object_gets_additional_false(self):
        from agents.task.utils import fix_schema_for_provider
        schema = _base_nested_object()
        result = fix_schema_for_provider(copy.deepcopy(schema), "anthropic")
        assert result["additionalProperties"] is False
        assert result["properties"]["config"]["additionalProperties"] is False

    def test_openai_array_items_object_gets_additional_false(self):
        from agents.task.utils import fix_schema_for_provider
        schema = _base_array_of_objects()
        result = fix_schema_for_provider(copy.deepcopy(schema), "openai")
        assert result["items"]["additionalProperties"] is False

    def test_anthropic_array_items_object_gets_additional_false(self):
        from agents.task.utils import fix_schema_for_provider
        schema = _base_array_of_objects()
        result = fix_schema_for_provider(copy.deepcopy(schema), "anthropic")
        assert result["items"]["additionalProperties"] is False

    def test_already_correct_unchanged(self):
        from agents.task.utils import fix_schema_for_provider
        schema = _already_correct()
        original = copy.deepcopy(schema)
        result = fix_schema_for_provider(copy.deepcopy(schema), "openai")
        assert result["additionalProperties"] is False
        # Structure preserved
        assert result["properties"] == original["properties"]

    def test_explicit_true_preserved_openai(self):
        """additionalProperties: True must NOT be overwritten."""
        from agents.task.utils import fix_schema_for_provider
        schema = _explicit_true()
        result = fix_schema_for_provider(copy.deepcopy(schema), "openai")
        assert result["additionalProperties"] is True

    def test_explicit_true_preserved_anthropic(self):
        from agents.task.utils import fix_schema_for_provider
        schema = _explicit_true()
        result = fix_schema_for_provider(copy.deepcopy(schema), "anthropic")
        assert result["additionalProperties"] is True

    def test_openai_and_anthropic_produce_identical_output(self):
        """Both providers should produce byte-identical results (no provider-specific branching)."""
        from agents.task.utils import fix_schema_for_provider
        schema = _base_nested_object()
        openai_result = fix_schema_for_provider(copy.deepcopy(schema), "openai")
        anthropic_result = fix_schema_for_provider(copy.deepcopy(schema), "anthropic")
        assert openai_result == anthropic_result


# ---------------------------------------------------------------------------
# Tests for backward-compat wrappers
# ---------------------------------------------------------------------------

class TestWrappers:
    def test_fix_openai_schema_is_thin_wrapper(self):
        """fix_openai_schema must delegate to fix_schema_for_provider (not a full duplicate)."""
        import inspect
        from agents.task import utils
        src = inspect.getsource(utils.fix_openai_schema)
        # A thin wrapper has exactly one statement in the body after any docstring.
        # We check the source length is short (< 300 chars) and calls fix_schema_for_provider.
        assert "fix_schema_for_provider" in src, (
            "fix_openai_schema must delegate to fix_schema_for_provider"
        )
        # Body lines (excluding def line and possible docstring) should be <= 3 lines
        body_lines = [
            line for line in src.splitlines()
            if line.strip() and not line.strip().startswith('def ')
            and not line.strip().startswith('"""')
            and not line.strip().startswith("'''")
        ]
        assert len(body_lines) <= 3, (
            f"fix_openai_schema should be a thin wrapper, got {len(body_lines)} non-blank body lines"
        )

    def test_fix_anthropic_schema_is_thin_wrapper(self):
        import inspect
        from agents.task import utils
        src = inspect.getsource(utils.fix_anthropic_schema)
        assert "fix_schema_for_provider" in src, (
            "fix_anthropic_schema must delegate to fix_schema_for_provider"
        )
        body_lines = [
            line for line in src.splitlines()
            if line.strip() and not line.strip().startswith('def ')
            and not line.strip().startswith('"""')
            and not line.strip().startswith("'''")
        ]
        assert len(body_lines) <= 3, (
            f"fix_anthropic_schema should be a thin wrapper, got {len(body_lines)} non-blank body lines"
        )

    def test_fix_openai_schema_output_matches_provider_fn(self):
        from agents.task.utils import fix_openai_schema, fix_schema_for_provider
        schema = _base_nested_object()
        assert fix_openai_schema(copy.deepcopy(schema)) == fix_schema_for_provider(
            copy.deepcopy(schema), "openai"
        )

    def test_fix_anthropic_schema_output_matches_provider_fn(self):
        from agents.task.utils import fix_anthropic_schema, fix_schema_for_provider
        schema = _base_nested_object()
        assert fix_anthropic_schema(copy.deepcopy(schema)) == fix_schema_for_provider(
            copy.deepcopy(schema), "anthropic"
        )

    def test_fix_openai_schema_nested_object(self):
        schema = _base_nested_object()
        result = fix_openai_schema(copy.deepcopy(schema))
        assert result["additionalProperties"] is False
        assert result["properties"]["config"]["additionalProperties"] is False

    def test_fix_anthropic_schema_nested_object(self):
        schema = _base_nested_object()
        result = fix_anthropic_schema(copy.deepcopy(schema))
        assert result["additionalProperties"] is False
        assert result["properties"]["config"]["additionalProperties"] is False

    def test_fix_openai_schema_explicit_true(self):
        schema = _explicit_true()
        result = fix_openai_schema(copy.deepcopy(schema))
        assert result["additionalProperties"] is True

    def test_fix_anthropic_schema_explicit_true(self):
        schema = _explicit_true()
        result = fix_anthropic_schema(copy.deepcopy(schema))
        assert result["additionalProperties"] is True

    def test_fix_openai_anyof(self):
        schema = {
            "type": "object",
            "anyOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}},
                {"type": "object", "properties": {"b": {"type": "integer"}}},
            ],
        }
        result = fix_openai_schema(copy.deepcopy(schema))
        assert result["additionalProperties"] is False
        for sub in result["anyOf"]:
            assert sub["additionalProperties"] is False

    def test_fix_anthropic_anyof(self):
        schema = {
            "type": "object",
            "anyOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}},
            ],
        }
        result = fix_anthropic_schema(copy.deepcopy(schema))
        assert result["additionalProperties"] is False
        assert result["anyOf"][0]["additionalProperties"] is False
