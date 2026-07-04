"""Phase 0.1 — in-session H-MEM placement (cache-prefix fix).

The per-step-changing hierarchical-memory block was appended to the FOUNDATION,
ahead of the whole conversation (retrieval.py). Because it changes every step, it
broke the cacheable prefix: every message after it (skills tail + all conversation)
was re-priced each step.

With HMEM_TAIL_PLACEMENT on, the H-MEM block is relocated to a dynamic SUFFIX after
the conversation, so the stable foundation + growing conversation form a cacheable
prefix and only the small H-MEM suffix is reprocessed. Legacy (off) is byte-identical
to before. The system message must stay messages[0] either way; H-MEM content must
still reach the model in both modes.
"""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.messages import retrieval as R
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import SystemMessage


class _TCM:
    def get_context_injection(self, session_id):
        return "HMEM_MARKER findings so far"


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    mm = MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
        task_context_manager=_TCM(), session_id="s1",
    )
    return mm


def _text(m):
    return m.content if isinstance(m.content, str) else str(m.content)


def _index_of(msgs, marker):
    for i, m in enumerate(msgs):
        if marker in _text(m):
            return i
    return -1


def test_legacy_places_hmem_before_conversation(monkeypatch):
    monkeypatch.setattr(R, "hmem_tail_placement", lambda: False)
    mm = _mm()
    mm.add_human_message("CONVO_MARKER turn")
    msgs = mm.get_messages_for_llm()
    hmem_i = _index_of(msgs, "HMEM_MARKER")
    convo_i = _index_of(msgs, "CONVO_MARKER")
    assert hmem_i != -1 and convo_i != -1
    assert hmem_i < convo_i  # H-MEM is in the foundation, ahead of conversation


def test_tail_placement_puts_hmem_after_conversation(monkeypatch):
    monkeypatch.setattr(R, "hmem_tail_placement", lambda: True)
    mm = _mm()
    mm.add_human_message("CONVO_MARKER turn")
    msgs = mm.get_messages_for_llm()
    hmem_i = _index_of(msgs, "HMEM_MARKER")
    convo_i = _index_of(msgs, "CONVO_MARKER")
    assert hmem_i != -1 and convo_i != -1
    assert hmem_i > convo_i  # H-MEM relocated to the dynamic suffix


def test_system_message_first_in_both_modes(monkeypatch):
    for mode in (False, True):
        monkeypatch.setattr(R, "hmem_tail_placement", lambda: mode)
        mm = _mm()
        mm.add_human_message("hello")
        msgs = mm.get_messages_for_llm()
        assert isinstance(msgs[0], SystemMessage)
        assert _index_of(msgs, "HMEM_MARKER") != -1  # H-MEM still present
