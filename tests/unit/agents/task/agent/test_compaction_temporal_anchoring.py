"""Phase 0.4 — temporal anchoring in the compaction summary prompt (Hermes B3).

A summarized middle that records an action as still-pending ("todo: email John")
causes a long/recurring task to RE-RUN finished work after rotation. The summary
prompt must instruct the model to record completed actions as DONE (past tense, with
outcome) and to keep them OUT of In Progress / Remaining Work.
"""
import logging

from modules.llm.messages import HumanMessage
from agents.task.agent.messages.compactor import CompactorMixin


class _Harness(CompactorMixin):
    def __init__(self):
        self.logger = logging.getLogger("test_temporal_anchoring")
        self.max_input_tokens = 1000


def test_prompt_contains_temporal_anchoring_instruction():
    h = _Harness()
    prompt = h._build_compaction_prompt([HumanMessage(content="did some work")])
    low = prompt.lower()
    # Records completed work as done and does not carry it forward as pending.
    assert "completed" in low
    assert "re-run" in low or "re-execute" in low or "redo" in low
    assert "remaining work" in low  # section still present
