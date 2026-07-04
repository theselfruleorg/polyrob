"""Two-turn end-to-end conversation via Conversation.respond() (Phase R4).

This is the integration test that proves the first-class conversation path works
against a REAL agent: real Conversation, real set_turn_input/append_user_turn,
real MessageManager history, real Agent.run() loop, real done-action execution
through the Controller/Registry, and real extract_answer. The ONLY thing stubbed
is the LLM decision boundary (get_next_action) so no network call happens — the
stub builds its answer by reading the REAL accumulated message history, which is
how the test proves turn-2 sees turn-1's context.

What this guards:
  - respond() returns the answer synchronously (no fire-and-forget).
  - Message history accumulates across turns (both user turns present, in order).
  - The resume-FSM "PRIORITY INPUT" position-1 injection is NOT used — set_turn_input
    appends directly, so the _drain_user_messages() queue stays empty.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agents.task.agent.conversation import Conversation
from agents.task.agent.views import AgentBrain, AgentOutput
from modules.llm.messages import HumanMessage


def _temp_env(monkeypatch, tmp_path):
    """Point all data paths at a tmp dir so we don't touch /opt/rob or the repo."""
    for k in ("DATA_DIR", "DATA_ROOT", "CHARACTERS_DIR", "KNOWLEDGE_DIR",
              "CACHE_DIR", "DB_PATH", "TELEMETRY_DATA_DIR"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    # Keep the run loop lean: no stall monitor, no GIF, no vision screenshots.
    monkeypatch.setenv("LOG_LEVEL", "ERROR")


# Synthetic per-step control content the agent injects into the LLM input (state
# snapshot, prior-step memory). Not genuine user turns — skip when finding the
# last real user message so the echo reflects the actual conversational input.
_CONTROL_PREFIXES = ("[CURRENT STATE]", "[MEMORY FROM PREVIOUS STEP]")


def _latest_human_text(messages):
    """Return the text of the last *genuine* user HumanMessage (or '').

    Skips the agent's injected per-step control messages so the echo reflects the
    real conversational turn rather than the state snapshot the agent appends last.
    """
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                content = " ".join(p for p in parts if p)
            if isinstance(content, str):
                if content.lstrip().startswith(_CONTROL_PREFIXES):
                    continue
                return content
    return ""


def _count_human_messages(messages):
    return sum(1 for m in messages if isinstance(m, HumanMessage))


def _make_llm_stub(agent):
    """Build a get_next_action stub that echoes the REAL latest user turn.

    It reads the agent's live message history (NOT a canned value), so the echo
    can only reflect "second task" on turn 2 if the run loop actually appended
    turn-1 and turn-2 into the same accumulating history. It then returns a real
    AgentOutput whose single action is a real `done` ActionModel built from the
    real registry — the rest of the step (Controller.multi_act -> done() ->
    ActionResult(is_done=True)) stays untouched.
    """
    # Real ActionModel type from the real registry of THIS agent's controller.
    action_model_cls = agent.controller.registry.create_action_model()

    async def fake_get_next_action(input_messages):
        # input_messages is what the real MessageManager assembled for the LLM.
        latest = _latest_human_text(input_messages)
        human_count = _count_human_messages(input_messages)
        echo = f"echo[{human_count}]: {latest}"

        brain = AgentBrain(
            evaluation_previous_goal="N/A",
            memory=f"saw {human_count} user messages; latest='{latest}'",
            next_goal="finish this turn",
        )
        # Build a real `done` action via the real ActionModel (validates params).
        # DoneAction only accepts `text` (aliased `message`); no `success` field.
        action = action_model_cls(done={"text": echo})
        return AgentOutput(current_state=brain, action=[action])

    return fake_get_next_action


@pytest.mark.asyncio
async def test_conversation_two_turns_end_to_end(monkeypatch, tmp_path):
    _temp_env(monkeypatch, tmp_path)

    # No network during LLM client init (matches the established test pattern).
    with patch("modules.llm.llm_manager.LLMManager._initialize", AsyncMock()):
        from core.bootstrap import build_cli_container
        container = await build_cli_container(log_level="ERROR")

    task_agent = container.get_agent("task_agent")
    assert task_agent is not None, "TaskAgent must be in the CLI container"

    # Create a real session (builds a real SessionOrchestrator with real tools).
    request = {
        "task": "interactive conversation",
        "model": "gemini-2.5-flash",
        "provider": "gemini",
        "tools": ["filesystem", "task"],  # pure-python, no browser/torch
        "max_steps": 2,
        "temperature": 0.0,
        "use_vision": False,
    }
    session_info = await task_agent.create_session(
        user_id="local", request=request, skip_credit_check=True,
    )
    session_id = session_info["id"]
    orchestrator = task_agent.get_orchestrator(session_id)
    assert orchestrator is not None

    # Build a REAL Agent on the real orchestrator (no LLM connection needed —
    # we override its decision method below). A dummy chat model satisfies the
    # constructor's model-name introspection without any network calls.
    class _DummyChat:
        model_name = "gemini-2.5-flash"

        async def ainvoke(self, *a, **k):  # never called — get_next_action is stubbed
            raise AssertionError("LLM must not be invoked in this test")

    agent = await orchestrator.create_agent(
        task="interactive conversation",
        llm=_DummyChat(),
        agent_name="executor",
        use_vision=False,
        max_actions_per_step=5,
    )

    # No stall monitor / heavy run-loop branches.
    agent.stall_timeout_seconds = None
    agent.generate_gif = False
    agent.validate_output = False

    # STUB ONLY THE LLM DECISION. Everything else stays real.
    agent.get_next_action = _make_llm_stub(agent)

    # Spy on the PRIORITY-input queue drain to prove it stays empty (the resume
    # FSM path is NOT taken — set_turn_input appends directly to history).
    real_drain = agent._drain_user_messages
    drained_batches = []

    async def spy_drain():
        msgs = await real_drain()
        drained_batches.append(list(msgs))
        return msgs

    agent._drain_user_messages = spy_drain

    convo = Conversation(agent)

    # ---- Turn 1 ----
    a1 = await convo.respond("first task", max_steps=2)
    history_after_t1 = list(agent.message_manager.get_messages())

    # ---- Turn 2 ----
    a2 = await convo.respond("second task", max_steps=2)
    history_after_t2 = list(agent.message_manager.get_messages())

    # --- Synchronous, non-trivial answers ---
    assert isinstance(a1, str) and a1.strip(), f"turn-1 answer empty: {a1!r}"
    assert isinstance(a2, str) and a2.strip(), f"turn-2 answer empty: {a2!r}"

    # --- Conversation owns both turns in order ---
    assert len(convo.turns) == 2
    assert convo.turns[0].user == "first task"
    assert convo.turns[0].assistant == a1
    assert convo.turns[1].user == "second task"
    assert convo.turns[1].assistant == a2

    # --- Continuity: BOTH user turns live in the real history, in order ---
    human_texts_t2 = [
        m.content for m in history_after_t2
        if isinstance(m, HumanMessage) and isinstance(m.content, str)
    ]
    assert "first task" in human_texts_t2, human_texts_t2
    assert "second task" in human_texts_t2, human_texts_t2
    assert human_texts_t2.index("first task") < human_texts_t2.index("second task")

    # History grew across turns (turn-1 message survived into turn-2 processing).
    assert _count_human_messages(history_after_t1) >= 1
    assert _count_human_messages(history_after_t2) > _count_human_messages(history_after_t1)

    # --- Turn-2 echo reflects the accumulated history, not just turn 1 ---
    assert "second task" in a2, f"turn-2 echo did not see turn-2 input: {a2!r}"
    # The echoed human count must be strictly larger on turn 2 (history accumulated).
    assert "echo[" in a1 and "echo[" in a2
    count_t1 = int(a1[a1.index("echo[") + 5: a1.index("]")])
    count_t2 = int(a2[a2.index("echo[") + 5: a2.index("]")])
    assert count_t2 > count_t1, f"history did not accumulate: t1={count_t1}, t2={count_t2}"

    # --- PRIORITY-input path NOT taken: the drain queue stayed empty every call ---
    assert drained_batches, "drain was never called — run loop did not execute"
    assert all(batch == [] for batch in drained_batches), (
        f"resume-FSM queue was non-empty (PRIORITY INPUT path taken): {drained_batches}"
    )
    # And no message in history carries the position-1 task-directive framing.
    for m in history_after_t2:
        content = m.content if isinstance(m.content, str) else ""
        assert "PRIORITY INPUT" not in content, "PRIORITY INPUT framing leaked into history"
