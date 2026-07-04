"""WS-1.3: systematic camelCase→snake_case field reconciliation.

kimi-k2.6 emits camelCase tool params (``filePath``) where ROB's Pydantic param
models use snake_case (``file_path``). Rather than grow the hand-maintained
whack-a-mole map, the casing class of error is fixed generically — but ONLY when
the snake_case form is an actual field of the target model, so genuinely-
camelCase tool params (e.g. some MCP servers) are left untouched.
"""

from __future__ import annotations

from agents.task.utils_json import camel_to_snake, reconcile_field_names_to_model


def test_camel_to_snake_basic():
    assert camel_to_snake("filePath") == "file_path"
    assert camel_to_snake("maxResults") == "max_results"
    assert camel_to_snake("userID") == "user_id"
    assert camel_to_snake("URL") == "url"


def test_camel_to_snake_idempotent_for_snake():
    assert camel_to_snake("file_path") == "file_path"
    assert camel_to_snake("query") == "query"
    assert camel_to_snake("") == ""


def test_reconcile_renames_when_snake_is_a_model_field():
    fields = {"file_path", "content"}
    out = reconcile_field_names_to_model({"filePath": "README.md"}, fields)
    assert out == {"file_path": "README.md"}


def test_reconcile_leaves_genuine_camel_params_untouched():
    # The MCP-safety guarantee: snake_case form is NOT a model field → keep as-is.
    fields = {"someCamelField", "other"}
    params = {"someCamelField": 1}
    assert reconcile_field_names_to_model(params, fields) == params


def test_reconcile_does_not_clobber_existing_snake_key():
    # If both filePath and file_path are present, don't overwrite the snake one.
    fields = {"file_path"}
    params = {"filePath": "a", "file_path": "b"}
    out = reconcile_field_names_to_model(params, fields)
    assert out["file_path"] == "b"


def test_reconcile_noop_for_empty():
    assert reconcile_field_names_to_model({}, {"file_path"}) == {}
    assert reconcile_field_names_to_model({"x": 1}, set()) == {"x": 1}


def test_reconcile_keeps_already_valid_fields():
    fields = {"file_path", "content"}
    params = {"file_path": "x", "content": "y"}
    assert reconcile_field_names_to_model(params, fields) == params
