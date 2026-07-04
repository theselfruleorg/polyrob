"""P9 pass-12 — tool-message repair split out of tool_call_builder.py."""


def test_repair_functions_reexported_from_tool_call_builder():
    # Backward-compat: existing call sites import these from tool_call_builder.
    from agents.task.agent.message_manager import tool_call_builder as tcb
    from agents.task.agent.message_manager import tool_message_repair as tmr
    for name in ("detect_and_remove_duplicate_tool_calls", "repair_tool_message_pairs",
                 "validate_tool_message_pairs", "repair_and_normalize"):
        assert getattr(tcb, name) is getattr(tmr, name)


def test_tool_call_builder_still_owns_builder():
    from agents.task.agent.message_manager.tool_call_builder import ToolCallBuilder, StandardToolCall
    assert ToolCallBuilder is not None and StandardToolCall is not None


def test_validate_tool_message_pairs_on_empty_is_true():
    from agents.task.agent.message_manager.tool_message_repair import validate_tool_message_pairs
    # no tool calls => trivially valid
    assert validate_tool_message_pairs([]) is True


def test_no_circular_import_either_order():
    # importing the repair module first must not deadlock/raise
    import importlib
    import agents.task.agent.message_manager.tool_message_repair as a  # noqa
    import agents.task.agent.message_manager.tool_call_builder as b  # noqa
    importlib.reload(a)
    assert hasattr(b, "repair_and_normalize")
