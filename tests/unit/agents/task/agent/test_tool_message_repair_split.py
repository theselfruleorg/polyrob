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
    # Cold imports belong in a fresh interpreter: reload changes function
    # identities while import-time reexports elsewhere retain their originals.
    import subprocess
    import sys

    prefix = "agents.task.agent.message_manager."
    for first, second in (("tool_message_repair", "tool_call_builder"),
                          ("tool_call_builder", "tool_message_repair")):
        subprocess.run(
            [sys.executable, "-c",
             "import importlib; "
             f"importlib.import_module({prefix + first!r}); "
             f"importlib.import_module({prefix + second!r})"],
            check=True, capture_output=True, text=True,
        )
