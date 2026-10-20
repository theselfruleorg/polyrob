"""2026-10-14 review C-1: recall, <live-health> (the spend limits) and the activity
digests are one-shot ephemerals. They were consumed by the FIRST response even when
that response called no tool — so the step that then swapped never saw the live
limits (runs f1bc0c0f / fe553b6b; one declared $10 and was refused).

A step with no action now re-queues them like a failed call; the step that acts
consumes them. A per-call message (format hint, state message) is never re-queued.
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.core.step import StepMixin
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import HumanMessage

LIVE = "<live-health>per-tx cap $5, daily $20</live-health>"


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(llm=llm, task="t", action_descriptions="acts",
                          system_prompt_class=SystemPrompt, max_input_tokens=8000,
                          session_id="s-c1")


def _texts(msgs):
    return [m.content if isinstance(m.content, str) else str(m.content) for m in msgs]


class _Agent(StepMixin):
    def __init__(self, mm, outputs):
        self.message_manager = mm
        self.logger = logging.getLogger("t-c1")
        self.tool_call_tracker = None
        self.state = SimpleNamespace(n_steps=1)
        self._outputs = list(outputs)
        self.seen = []

    async def get_next_action(self, _input_messages):
        # what the real path does: assemble (consuming the ephemerals), then call
        self.seen.append(_texts(self.message_manager.get_messages_for_llm()))
        return self._outputs.pop(0)


def _out(actions):
    return SimpleNamespace(action=actions, current_state=None)


def test_planning_turn_then_action_turn_both_see_live_health():
    mm = _mm()
    mm.use_native_tools = False
    mm.push_ephemeral_message(HumanMessage(content=LIVE))
    hint = HumanMessage(content="FORMAT HINT")
    hint.metadata = {"per_call": True}
    mm.push_ephemeral_message(hint)
    agent = _Agent(mm, [_out([]), _out([{"done": {"text": "ok"}}]), _out([])])

    asyncio.run(agent._call_llm([]))           # step 1: a tool-free planning turn
    asyncio.run(agent._call_llm([]))           # step 2: the acting step
    asyncio.run(agent._call_llm([]))           # step 3: consumed by step 2

    assert any(LIVE in t for t in agent.seen[0])
    assert any(LIVE in t for t in agent.seen[1]), "the acting step must see the limits"
    assert not any(LIVE in t for t in agent.seen[2]), "consumed once an action ran"
    # the per-call hint rode step 1 only — never re-queued beside a fresh one
    assert sum("FORMAT HINT" in t for t in agent.seen[1]) == 0


def test_restore_drops_the_stale_state_message():
    mm = _mm()
    state = HumanMessage(content="CURRENT STATE step 1")
    state.metadata = {"state_message": True}
    mm.push_ephemeral_message(state)
    mm.push_ephemeral_message(HumanMessage(content=LIVE))
    mm.get_messages_for_llm()
    mm.restore_ephemeral_on_failure()
    again = _texts(mm.get_messages_for_llm())
    assert any(LIVE in t for t in again)
    assert not any("CURRENT STATE step 1" in t for t in again)
