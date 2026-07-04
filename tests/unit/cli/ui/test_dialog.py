"""Unit tests for cli.ui.dialog — the dialog-first pure predicates."""

from __future__ import annotations

from cli.ui import dialog

# Real send_message action shape (captured feed, §0 amendment 3): action_type
# is "send_message", name is "message".
_MSG_ACTION = {
    "action_type": "send_message",
    "name": "message",
    "service": "send",
    "params": {"text": "## Hello\n\nfull body", "wait_for_response": True,
               "timeout_seconds": 300},
}
_NON_MSG_ACTION = {
    "action_type": "read_file",
    "name": "read_file",
    "service": "fs",
    "params": {"file_path": "x.py"},
}


# ---------------------------------------------------------------------------
# Plumbing strings
# ---------------------------------------------------------------------------


def test_plumbing_strings_recognised():
    assert dialog.is_plumbing_string(
        "Message sent to user. Task paused - will resume when user responds."
    )
    assert dialog.is_plumbing_string("Message sent to user (non-blocking)")
    assert dialog.is_plumbing_string("Session completed successfully")
    # em-dash variant of the pause receipt
    assert dialog.is_plumbing_string(
        "Message sent to user. Task paused — will resume when user responds."
    )


def test_plumbing_strings_ignore_real_answers():
    assert not dialog.is_plumbing_string("The capital of France is Paris.")
    assert not dialog.is_plumbing_string("")
    assert not dialog.is_plumbing_string(None)


# ---------------------------------------------------------------------------
# Echo reasoning / memory
# ---------------------------------------------------------------------------


def test_echo_reasoning_prefix():
    assert dialog.is_echo_reasoning(
        "Executed: send_message(text=hello, wait_for_response=False)"
    )
    assert not dialog.is_echo_reasoning("I should read the config first.")
    assert not dialog.is_echo_reasoning("")
    assert not dialog.is_echo_reasoning(None)


def test_echo_memory():
    assert dialog.is_echo_memory(
        "send_message(text=hello, wait_for_response=False)→Message sent to user"
    )
    assert dialog.is_echo_memory("message(text=hi)→ok")
    assert not dialog.is_echo_memory("tracking the auth refactor")
    assert not dialog.is_echo_memory("")


# ---------------------------------------------------------------------------
# send_message extraction
# ---------------------------------------------------------------------------


def test_is_send_message_action_matches_action_type_and_name():
    assert dialog.is_send_message_action(_MSG_ACTION)
    assert dialog.is_send_message_action({"name": "message", "params": {}})
    assert dialog.is_send_message_action({"name": "send_message", "params": {}})
    assert not dialog.is_send_message_action(_NON_MSG_ACTION)
    assert not dialog.is_send_message_action({})


def test_message_text_returns_full_untruncated():
    assert dialog.message_text(_MSG_ACTION) == "## Hello\n\nfull body"
    assert dialog.message_text(_NON_MSG_ACTION) is None
    # empty text → None (nothing to render)
    assert dialog.message_text({"name": "message", "params": {"text": "  "}}) is None
    assert dialog.message_text({"name": "message", "params": {}}) is None


def test_find_message_text_first_match():
    actions = [_NON_MSG_ACTION, _MSG_ACTION]
    assert dialog.find_message_text(actions) == "## Hello\n\nfull body"
    assert dialog.find_message_text([_NON_MSG_ACTION]) is None
    assert dialog.find_message_text([]) is None


def test_step_is_message_only():
    assert dialog.step_is_message_only([_MSG_ACTION])
    assert not dialog.step_is_message_only([_MSG_ACTION, _NON_MSG_ACTION])
    assert not dialog.step_is_message_only([_NON_MSG_ACTION])
    # empty → not a message-only step
    assert not dialog.step_is_message_only([])


# ---------------------------------------------------------------------------
# Brain-state detection (planning-turn content, NOT a chat message)
# ---------------------------------------------------------------------------

# kimi-k2.6 (and other models) emit a tool-free planning turn whose CONTENT is
# the brain-state — either wrapped in {"current_state": {...}} or as bare brain
# keys, or as the Memory:/Next:/Reasoning: text echo. None of these are a chat
# message; the dialog renderer must demote them, not print them as "rob".

_BRAIN_WRAPPED = (
    '{"current_state": {"evaluation_previous_goal": "N/A - starting fresh", '
    '"memory": "User wants a repo review.", '
    '"next_goal": "List the root directory, then explore key files.", '
    '"reasoning": "Understand layout first."}}'
)
_BRAIN_BARE = (
    '{"page_summary":"","evaluation_previous_goal":"Pending",'
    '"memory":"Synthesis pending","next_goal":"Read modules/llm","reasoning":"x"}'
)
_BRAIN_TEXT = "Memory: tracking the review\nNext: list the root directory\nReasoning: layout first"


def test_is_brain_state_detects_json_shapes():
    assert dialog.is_brain_state(_BRAIN_WRAPPED)
    assert dialog.is_brain_state(_BRAIN_BARE)
    assert dialog.is_brain_state(_BRAIN_TEXT)


def test_is_brain_state_ignores_real_messages():
    # A genuine send_message body is markdown prose, never brain-state.
    assert not dialog.is_brain_state("## Repo Review\n\nThe project is a FastAPI app.")
    assert not dialog.is_brain_state("The capital of France is Paris.")
    # A lone JSON object that isn't brain-shaped must not be swallowed.
    assert not dialog.is_brain_state('{"result": 42, "ok": true}')
    assert not dialog.is_brain_state("")
    assert not dialog.is_brain_state(None)


def test_brain_planning_line_prefers_next_goal():
    assert (
        dialog.brain_planning_line(_BRAIN_WRAPPED)
        == "List the root directory, then explore key files."
    )
    assert dialog.brain_planning_line(_BRAIN_BARE) == "Read modules/llm"
    assert dialog.brain_planning_line(_BRAIN_TEXT) == "list the root directory"


def test_brain_planning_line_falls_back_past_placeholders():
    # next_goal is a placeholder → fall through to reasoning.
    brain = (
        '{"current_state": {"next_goal": "Pending", '
        '"reasoning": "Waiting on the previous read.", "memory": "N/A"}}'
    )
    assert dialog.brain_planning_line(brain) == "Waiting on the previous read."


def test_brain_planning_line_none_for_non_brain():
    assert dialog.brain_planning_line("## Hello\n\nbody") is None
    assert dialog.brain_planning_line("") is None
    assert dialog.brain_planning_line(None) is None


# ---------------------------------------------------------------------------
# WS-3.2: stray Kimi control-token backstop (session c1c81b26)
# ---------------------------------------------------------------------------

# The latest kimi leak: brain JSON + trailing <|tool_call_end|> tokens (no begin
# block). Without stripping, the trailing tokens make the JSON not end with "}",
# so is_brain_state would miss it and the dump would render as a "rob" bubble.
_BRAIN_WITH_STRAY_TOKENS = (
    '{"current_state": {"evaluation_previous_goal":"Success",'
    '"memory":"Reviewed the folder.","next_goal":"Deliver the review",'
    '"reasoning":"All files read."}} '
    "<|tool_call_end|> <|tool_calls_section_end|>"
)


def test_strip_control_tokens_removes_kimi_tokens():
    assert (
        dialog.strip_control_tokens(_BRAIN_WITH_STRAY_TOKENS)
        == '{"current_state": {"evaluation_previous_goal":"Success",'
        '"memory":"Reviewed the folder.","next_goal":"Deliver the review",'
        '"reasoning":"All files read."}}'
    )
    # No-op for clean content (every other provider).
    assert dialog.strip_control_tokens("## Hello\n\nbody") == "## Hello\n\nbody"
    assert dialog.strip_control_tokens("") == ""
    assert dialog.strip_control_tokens(None) is None


def test_is_brain_state_detects_brain_with_stray_tokens():
    # The render-layer backstop: the dump must be recognised as brain-state so it
    # is demoted, never printed as the agent's voice.
    assert dialog.is_brain_state(_BRAIN_WITH_STRAY_TOKENS)


def test_brain_planning_line_handles_stray_tokens():
    assert (
        dialog.brain_planning_line(_BRAIN_WITH_STRAY_TOKENS) == "Deliver the review"
    )


# --- B4: brain JSON + trailing tool-call junk (render-layer backstop) ---------

_BRAIN_WITH_PYCALL = (
    '{"current_state": {"evaluation_previous_goal": "Success", '
    '"memory": "User greeted me.", "next_goal": "Reply to the greeting.", '
    '"reasoning": "Be friendly."}} '
    'done(text="Hey! I\'m doing well, thanks for asking.")'
)

_BRAIN_WITH_STRAY_INVOKE = '{"memory": "x", "next_goal": "Deliver result"} </invoke>'


def test_is_brain_state_detects_brain_with_trailing_pycall():
    # B4 leak #1: the client recovery should strip the done(...) call, but even
    # if a residue reaches the renderer it must be demoted, not shown as a bubble.
    assert dialog.is_brain_state(_BRAIN_WITH_PYCALL)
    assert dialog.brain_planning_line(_BRAIN_WITH_PYCALL) == "Reply to the greeting."


def test_is_brain_state_detects_brain_with_trailing_invoke_fragment():
    # B4 leak #2: brain JSON + a lone </invoke> fragment.
    assert dialog.is_brain_state(_BRAIN_WITH_STRAY_INVOKE)
    assert dialog.brain_planning_line(_BRAIN_WITH_STRAY_INVOKE) == "Deliver result"


def test_genuine_json_reply_is_not_brain_state():
    # A real (non-brain) JSON object must still NOT be classified as brain-state.
    assert not dialog.is_brain_state('{"answer": 42, "unit": "things"}')


# --- OR-2: DeepSeek (non-native JSON-fallback) wraps brain JSON in a ```json ---
# markdown fence. On a tool-free greeting the fenced brain-state was the model's
# entire content; without fence-stripping, .startswith("{") fails so is_brain_state
# returned False and the raw fenced JSON dumped as a "rob" bubble (headline leak).

_BRAIN_FENCED = (
    "```json\n"
    "{\n"
    '  "current_state": {\n'
    '    "evaluation_previous_goal": "Success - greeted user conversationally",\n'
    '    "memory": "User initiated a casual greeting.",\n'
    '    "next_goal": "Reply to greeting and offer assistance",\n'
    '    "reasoning": "Simple greeting -> respond warmly.",\n'
    '    "phase": "discovery"\n'
    "  }\n"
    "}\n"
    "```"
)

_BRAIN_FENCED_BARE = "```\n" + _BRAIN_WRAPPED + "\n```"


def test_is_brain_state_detects_fenced_json():
    # The DeepSeek leak: fenced brain JSON must be recognised and demoted, never
    # rendered as the agent's voice.
    assert dialog.is_brain_state(_BRAIN_FENCED)
    assert dialog.is_brain_state(_BRAIN_FENCED_BARE)


def test_brain_planning_line_handles_fenced_json():
    assert (
        dialog.brain_planning_line(_BRAIN_FENCED)
        == "Reply to greeting and offer assistance"
    )


def test_fenced_non_brain_json_is_not_brain_state():
    # A fenced but non-brain JSON object must still NOT be swallowed.
    assert not dialog.is_brain_state('```json\n{"answer": 42, "unit": "things"}\n```')


def test_fenced_prose_is_not_brain_state():
    # A genuine fenced code block of prose/code is a real reply, not brain-state.
    assert not dialog.is_brain_state("```python\nprint('hello world')\n```")


# --- FIX-C: a brain object FOLLOWED BY genuine prose is a mixed reply, NOT pure ---
# brain-state. Demoting it would eat the real reply. Trailing tool-call junk
# (done(...)/</invoke>) still counts as brain (B4 contract preserved above).

def test_is_redundant_recap_matches_bookkeeping():
    bubble = "Hey! Doing great, thanks for asking. How can I help?"
    assert dialog.is_redundant_recap("Responded to the user's greeting. No further action needed.", bubble)
    assert dialog.is_redundant_recap("Greeted the user conversationally; no task requested.", bubble)
    assert dialog.is_redundant_recap(bubble, bubble)  # identical


def test_is_redundant_recap_spares_real_answers():
    bubble = "Working on it…"
    # A genuine final answer is NOT a recap, even if short.
    assert not dialog.is_redundant_recap("The capital of France is Paris.", bubble)
    assert not dialog.is_redundant_recap("Here is your poem:\nWaves crash on the shore.", bubble)
    assert not dialog.is_redundant_recap("", bubble)
    assert not dialog.is_redundant_recap("Responded with details.", "")  # no bubble → not a recap


def test_brain_then_prose_is_not_brain_state():
    mixed = _BRAIN_WRAPPED + "\n\nHey there! I'm doing great — how can I help you today?"
    assert not dialog.is_brain_state(mixed)


def test_fenced_brain_then_prose_is_not_brain_state():
    mixed = "```json\n" + _BRAIN_WRAPPED + "\n```\n\nHello! What can I do for you?"
    assert not dialog.is_brain_state(mixed)
