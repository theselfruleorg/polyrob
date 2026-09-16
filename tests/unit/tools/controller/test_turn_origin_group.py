import types
from tools.controller.turn_origin import _is_forged_or_autonomous_turn


def test_group_turn_kind_is_forged():
    ctx = types.SimpleNamespace(is_sub_agent=False, role="orchestrator",
                                session_id="s", metadata={"turn_kind": "group"})
    assert _is_forged_or_autonomous_turn(ctx, types.SimpleNamespace()) is True


def test_genuine_turn_is_not_forged():
    ctx = types.SimpleNamespace(is_sub_agent=False, role="orchestrator",
                                session_id="s", metadata={"turn_kind": None})
    assert _is_forged_or_autonomous_turn(ctx, types.SimpleNamespace()) is False
