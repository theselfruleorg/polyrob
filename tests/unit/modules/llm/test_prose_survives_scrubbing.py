"""A reply that mentions a tag or a brace keeps its text (106 L10, L11)."""
from modules.llm.brain_scrubber import scrub_brain_blocks
from modules.llm.openrouter_client import recover_textual_tool_calls


def test_prose_that_mentions_the_tag_keeps_all_its_text():
    text = "The agent wraps a call in `<function_calls>`, and the rest of this reply stays."
    assert recover_textual_tool_calls(text, {"done"}) == (text, [])


def test_prose_that_mentions_a_stray_tag_keeps_it():
    text = "Close the block with </invoke> at the end."
    assert recover_textual_tool_calls(text, {"done"}) == (text, [])


def test_a_dump_after_a_brain_object_is_still_stripped():
    text = '{"current_state": {"next_goal": "x"}}<function_calls><write_file><arg_key>p'
    cleaned, calls = recover_textual_tool_calls(text, {"done"})
    assert "<function_calls" not in cleaned and calls == []


def test_a_dump_after_prose_is_still_stripped():
    text = "Here is the result.\n<function_calls><write_file><arg_key>p</arg_key></function_calls>"
    assert recover_textual_tool_calls(text, {"done"})[0] == "Here is the result."


def test_a_dump_alone_is_still_stripped():
    cleaned, _ = recover_textual_tool_calls("<function_calls><write_file><arg_key>p", {"done"})
    assert cleaned == ""


def test_prose_around_a_broken_brace_and_a_brain_block_survives():
    out = scrub_brain_blocks('Set {name}. {"current_state": {"next_goal": "x"}} Done.')
    assert "Set {name}." in out and "Done." in out and "current_state" not in out


def test_a_broken_brace_before_a_cut_brain_keeps_the_prose():
    out = scrub_brain_blocks('Set {name}. {"current_state": {"a": 1}, "next_go')
    assert out == "Set {name}."


def test_a_cut_brain_is_still_dropped():
    assert scrub_brain_blocks('Hello {"current_state": {"a": 1}, "next_go') == "Hello"
    assert scrub_brain_blocks('Hi {"next_goal": "x", "memory": "y') == "Hi"
