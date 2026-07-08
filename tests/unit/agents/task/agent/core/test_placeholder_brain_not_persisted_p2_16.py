"""P2-16 (intelligence-polish plan 2026-07-07): the pre-synthesis PLACEHOLDER brain
("Synthesis pending - will be generated after actions execute") must NOT be serialized
verbatim into the provider-visible AIMessage. Synthesis that replaces it runs AFTER the
atomic add, so the placeholder used to persist into history.
"""
import logging

import pytest

from agents.task.agent.core.result_processing import ResultProcessingMixin
from agents.task.agent.views import AgentBrain, AgentOutput
from tools.controller.registry.views import ActionModel
from tools.controller.types import ActionResult


class _CapturingMM:
    use_native_tools = True

    def __init__(self):
        self.captured = {}

    def add_tool_call_pair_atomic(self, *, ai_content, tool_calls, tool_responses):
        self.captured["ai_content"] = ai_content


class _Host(ResultProcessingMixin):
    use_native_tools = True

    def __init__(self, mm):
        self.message_manager = mm
        self.logger = logging.getLogger("p2_16")
        self.tool_call_tracker = None
        self.controller = None

    def _source_for_tool_call(self, *a, **k):
        return (None, None)


def _output(memory: str):
    brain = AgentBrain(evaluation_previous_goal="ok", memory=memory, next_goal="n")
    act = ActionModel()
    act._tool_call_id = "call_1"
    Custom = AgentOutput.type_with_custom_actions(ActionModel)
    return Custom(current_state=brain, action=[act.model_dump(exclude_unset=True)])


@pytest.mark.asyncio
async def test_placeholder_brain_not_persisted():
    mm = _CapturingMM()
    host = _Host(mm)
    out = _output("Synthesis pending - will be generated after actions execute")
    r = ActionResult(extracted_content="did the thing")
    r.tool_call_id = "call_1"
    await host._add_tool_messages([r], out, [{"id": "call_1", "name": "x"}])
    assert "Synthesis pending" not in mm.captured.get("ai_content", "")


@pytest.mark.asyncio
async def test_real_brain_still_serialized():
    mm = _CapturingMM()
    host = _Host(mm)
    out = _output("I searched and found the config file at /etc/app.conf")
    r = ActionResult(extracted_content="did the thing")
    r.tool_call_id = "call_1"
    await host._add_tool_messages([r], out, [{"id": "call_1", "name": "x"}])
    assert "found the config file" in mm.captured.get("ai_content", "")
