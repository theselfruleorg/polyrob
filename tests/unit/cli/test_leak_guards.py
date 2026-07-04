"""G5 — brain-state / reasoning leak-guard regression tests.

Locks the OR-7 / Kimi / NIM / B4 leak class: reasoning blocks, brain-state echoes,
leaked tool-call tokens, and trailing ``done(...)``/``<invoke>`` junk must NEVER reach
user-facing content. These give the autonomous loop's leak metric a contract — they
fail if the scrubber or the renderer's brain-state detection is weakened.
"""
from cli.ui.dialog import is_brain_state, strip_control_tokens
from modules.llm.think_scrubber import scrub_think_blocks


# --- reasoning blocks are scrubbed ----------------------------------------

def test_think_block_fully_removed():
    out = scrub_think_blocks("Hello <think>secret chain of thought</think> world")
    assert "<think>" not in out and "secret chain of thought" not in out
    assert "Hello" in out and "world" in out


def test_reasoning_and_thought_tags_removed():
    for tag in ("reasoning", "thinking", "thought"):
        out = scrub_think_blocks(f"<{tag}>internal {tag}</{tag}>visible")
        assert f"internal {tag}" not in out
        assert "visible" in out


# --- brain-state echoes are recognised (→ demoted, not shown verbatim) -----

def test_memory_next_reasoning_echo_is_brain_state():
    text = "Memory: did X\nNext: do Y\nReasoning: because Z"
    assert is_brain_state(text) is True


def test_brain_json_is_brain_state():
    assert is_brain_state('{"current_state": {"memory": "m", "next_goal": "g"}}') is True


def test_brain_json_with_trailing_done_call_is_brain_state():
    # B4 residue: brain JSON the client couldn't fully strip, with a leaked done()/<invoke>
    leaked = '{"memory": "m", "next_goal": "g"} done(text="hi")'
    assert is_brain_state(leaked) is True
    leaked2 = '{"memory": "m", "next_goal": "g"}<invoke name="x"></invoke>'
    assert is_brain_state(leaked2) is True


# --- leaked Kimi/NIM tool-call control tokens are stripped -----------------

def test_kimi_control_tokens_stripped():
    out = strip_control_tokens("answer<|tool_call_begin|><|tool_call_end|>")
    assert "<|tool_call_begin|>" not in out and "<|tool_call_end|>" not in out
    assert "answer" in out


# --- a genuine prose reply is NOT misclassified as brain-state -------------

def test_genuine_reply_not_brain_state():
    assert is_brain_state("Sure! Here is the summary you asked for.") is False
