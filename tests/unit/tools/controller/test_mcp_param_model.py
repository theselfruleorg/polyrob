"""WS-B5 — MCP JSON-schema -> dynamic Pydantic param model.

Characterizes existing flat behavior (must not regress) and adds nested-shape
preservation: arrays carry their item type; objects stay lenient (Dict[str, Any])
so a previously-valid payload is never rejected.
"""
import logging
from typing import get_args, get_origin

import agents.task.agent.service  # noqa: F401 — avoid import cycle
from tools.controller.service import Controller


def _bare_controller() -> Controller:
    c = object.__new__(Controller)
    c.logger = logging.getLogger("mcp-param-model-test")
    return c


def _model(schema):
    return _bare_controller()._create_param_model_from_schema("my_tool", schema)


# --- characterization: flat behavior preserved -------------------------------

def test_required_string_field():
    M = _model({"properties": {"q": {"type": "string"}}, "required": ["q"]})
    assert M(q="hi").q == "hi"


def test_optional_field_defaults_none():
    M = _model({"properties": {"n": {"type": "integer"}}})
    assert M().n is None


def test_required_field_missing_raises():
    import pytest
    from pydantic import ValidationError
    M = _model({"properties": {"q": {"type": "string"}}, "required": ["q"]})
    with pytest.raises(ValidationError):
        M()


# --- new: nested shape preservation ------------------------------------------

def test_typed_array_accepts_list_and_carries_item_type():
    M = _model({
        "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
        "required": ["ids"],
    })
    assert M(ids=[1, 2, 3]).ids == [1, 2, 3]
    # field annotation should be List[int], not bare list
    ann = M.model_fields["ids"].annotation
    assert get_origin(ann) is list
    assert get_args(ann) == (int,)


def test_object_field_accepts_nested_dict_leniently():
    M = _model({
        "properties": {"filter": {"type": "object", "properties": {"a": {"type": "string"}}}},
        "required": ["filter"],
    })
    # a previously-valid arbitrary dict (incl. unforeseen keys) must still pass
    val = {"a": "x", "unexpected": 9}
    assert M(filter=val).filter == val


def test_array_without_item_type_still_accepts_list():
    M = _model({"properties": {"xs": {"type": "array"}}, "required": ["xs"]})
    assert M(xs=["anything", 1, {"k": "v"}]).xs == ["anything", 1, {"k": "v"}]
