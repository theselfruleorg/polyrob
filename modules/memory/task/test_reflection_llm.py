"""REFLECTION_LLM_ENABLED routes phase consolidation through an aux model; off => concat."""
import logging
from modules.llm.messages import AIMessage
from modules.memory.task.task_context_manager import TaskContextManager


class _StubLLM:
    def __init__(self, reply="SYNTH SUMMARY", fail=False):
        self.reply, self.fail, self.calls = reply, fail, []
    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("down")
        return AIMessage(content=self.reply)


def _tcm(enabled, llm):
    tcm = TaskContextManager.__new__(TaskContextManager)  # bypass heavy __init__
    tcm.logger = logging.getLogger("test_reflection_llm")
    tcm.reflection_llm_enabled = enabled
    tcm.reflection_llm = llm
    return tcm


def test_consolidate_uses_aux_when_enabled():
    llm = _StubLLM()
    tcm = _tcm(True, llm)
    assert tcm._llm_consolidate(["found A", "found B"]) == "SYNTH SUMMARY"
    assert len(llm.calls) == 1


def test_consolidate_returns_none_when_disabled():
    tcm = _tcm(False, _StubLLM())
    assert tcm._llm_consolidate(["x"]) is None     # caller falls back to concat


def test_consolidate_failopen_on_error():
    tcm = _tcm(True, _StubLLM(fail=True))
    assert tcm._llm_consolidate(["x"]) is None     # error => None => concat fallback
