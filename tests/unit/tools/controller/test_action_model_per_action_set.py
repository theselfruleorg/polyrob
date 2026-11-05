"""102 N-1: one action model per action set.

``tool_call_to_action`` asks for the model on every tool call. A rebuild there ran
``model_json_schema`` per action and wrote one telemetry event and one feed file.
"""
from pydantic import BaseModel

from tools.controller.registry.service import Registry


class _P(BaseModel):
    x: int = 0


class _Q(BaseModel):
    y: int = 0


def _registry():
    reg = Registry()
    captured = []
    reg.telemetry = type("T", (), {"capture": lambda self, e, **k: captured.append(e)})()

    @reg.action("first action", param_model=_P)
    async def first(params: _P):
        return None

    return reg, captured


def test_five_tool_calls_build_one_class_and_one_event():
    reg, captured = _registry()
    classes = {id(reg.tool_call_to_action("first", {"x": 1}).__class__) for _ in range(5)}
    assert len(classes) == 1
    assert len(captured) == 1
    fn = captured[0].registered_functions[0]
    assert fn.name == "first" and not hasattr(fn, "params")


def test_a_new_action_gives_a_new_class_and_a_new_event():
    reg, captured = _registry()
    before = reg.create_action_model()

    @reg.action("second action", param_model=_Q)
    async def second(params: _Q):
        return None

    after = reg.create_action_model()
    assert after is not before
    assert "second" in after.model_fields
    assert len(captured) == 2
    assert reg.create_action_model() is after


def test_a_replaced_param_model_gives_a_new_class():
    reg, _ = _registry()
    before = reg.create_action_model()
    reg.registry.actions["first"].param_model = _Q
    assert reg.create_action_model() is not before
