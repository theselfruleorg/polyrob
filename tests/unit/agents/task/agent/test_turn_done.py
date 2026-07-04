"""Agent can answer one turn within a small step budget (R4)."""
import pytest
from agents.task.agent.conversation import extract_answer


def test_extract_answer_from_history():
    class _R:
        def __init__(self, is_done, content): self.is_done = is_done; self.extracted_content = content
    class _Hist:
        def __init__(self, results): self._results = results
        def __iter__(self): return iter(self._results)
    hist = _Hist([_R(False, "thinking"), _R(True, "the answer")])
    assert extract_answer(hist) == "the answer"


def test_extract_answer_empty():
    class _Hist:
        def __iter__(self): return iter([])
    assert extract_answer(_Hist()) == ""
