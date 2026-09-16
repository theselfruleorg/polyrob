"""_extract_chat_reply — the reply extractor behind chat_once, the telegram
surface and (as display fallback) RunOutcome.

Live root cause (goal 58a1385d18bf): priority-1 read ``agent.state.history``,
but AgentState has NO ``history`` field — the ledger lives on ``agent.history``
— so priority-1 ALWAYS AttributeError'd silently and priority-2 returned the
P2-16 placeholder AIMessage "Processing actions" (added to history AFTER the
clean "✅ Task Complete" message, so it wins the reverse scan)."""
from types import SimpleNamespace

from agents.task_agent_lite import TaskAgent
from modules.llm.messages import AIMessage


def _task_agent_with(orch):
    ta = TaskAgent.__new__(TaskAgent)
    ta._registry = SimpleNamespace(get=lambda sid: orch)
    return ta


def _mm(messages):
    managed = [SimpleNamespace(message=m) for m in messages]
    return SimpleNamespace(history=SimpleNamespace(messages=managed))


def test_priority1_reads_agent_history_not_state_history():
    """The done() text must come from agent.history (the real ledger attr) —
    AgentState has no history field, so state.history can never work."""
    hist = SimpleNamespace(
        is_done=lambda: True,
        final_result=lambda: "OUTCOME: BLOCKED — x402 payment request store unavailable",
    )
    agent = SimpleNamespace(
        history=hist,
        state=SimpleNamespace(n_steps=3),  # deliberately NO .history
        message_manager=None,
    )
    orch = SimpleNamespace(agents={"main": agent})
    ta = _task_agent_with(orch)
    assert "x402" in ta._extract_chat_reply("s1")


def test_priority2_skips_framework_placeholder_messages():
    """The placeholder AIMessage ('Processing actions') is framework-authored
    and must never be returned as the agent's reply; the clean completion
    message behind it wins."""
    agent = SimpleNamespace(
        history=SimpleNamespace(is_done=lambda: False, final_result=lambda: None),
        state=SimpleNamespace(n_steps=3),
        message_manager=_mm([
            AIMessage(content="✅ Task Complete\n\nWrote the report to workspace/report.md"),
            AIMessage(content="Processing actions"),
        ]),
    )
    orch = SimpleNamespace(agents={"main": agent})
    ta = _task_agent_with(orch)
    assert ta._extract_chat_reply("s1") == "Wrote the report to workspace/report.md"


def test_placeholder_never_returned_even_as_last_resort():
    agent = SimpleNamespace(
        history=SimpleNamespace(is_done=lambda: False, final_result=lambda: None),
        state=SimpleNamespace(n_steps=1),
        message_manager=_mm([AIMessage(content="Processing actions")]),
    )
    orch = SimpleNamespace(agents={"main": agent})
    ta = _task_agent_with(orch)
    assert ta._extract_chat_reply("s1") == ""


# ---------------------------------------------------------------------------
# C3 / F4 — which text is THE reply on the unbound path
#
# `_extract_chat_reply` preferred history.final_result() (= done's text) over
# the real send_message text, so the same missing concept presented twice: a
# duplicate on a bound surface, and the bookkeeping recap delivered INSTEAD of
# the answer on every unbound path (raw API, chat_once, /v1, a surface started
# without the bus).
#
# Ordering alone cannot fix it — done writes "✅ Task Complete\n\n<text>" into
# history and so IS the last AIMessage. The reply is recorded where it is
# published (core.surfaces.turn_reply) instead.
# ---------------------------------------------------------------------------

from core.surfaces import turn_reply  # noqa: E402

_RECAP = "Answered the owner's two questions via Telegram. No further action needed."
_ANSWER = "The capital of France is Paris."


def _orch_with(done_text=None, ai_texts=()):
    hist = SimpleNamespace(is_done=lambda: done_text is not None,
                           final_result=lambda: done_text)
    agent = SimpleNamespace(history=hist,
                            message_manager=_mm([AIMessage(content=t) for t in ai_texts]))
    return SimpleNamespace(agents={"a": agent})


def test_the_send_message_text_wins_over_the_done_recap():
    orch = _orch_with(done_text=_RECAP, ai_texts=[f"✅ Task Complete\n\n{_RECAP}"])
    turn_reply.mark_reply_published(orch, _ANSWER)
    assert _task_agent_with(orch)._extract_chat_reply("s1") == _ANSWER


def test_done_alone_is_still_returned():
    """No send_message this turn ⇒ done's text is all the user can get."""
    orch = _orch_with(done_text="Here is your answer.")
    assert _task_agent_with(orch)._extract_chat_reply("s1") == "Here is your answer."


def test_a_recorded_reply_does_not_leak_across_turns():
    orch = _orch_with(done_text="turn two answer")
    turn_reply.mark_reply_published(orch, "turn one answer")
    turn_reply.reset_turn(orch)
    assert _task_agent_with(orch)._extract_chat_reply("s1") == "turn two answer"


def test_brain_state_is_never_returned_as_a_recorded_reply():
    orch = _orch_with(done_text="clean done text")
    turn_reply.mark_reply_published(orch, '{"current_state": {"memory": "x", "next_goal": "y"}}')
    assert _task_agent_with(orch)._extract_chat_reply("s1") == "clean done text"


def test_a_blank_recorded_reply_falls_through():
    orch = _orch_with(done_text="clean done text")
    turn_reply.mark_reply_published(orch, "   ")
    assert _task_agent_with(orch)._extract_chat_reply("s1") == "clean done text"
