"""P2-14 (intelligence-polish plan 2026-07-07): one-shot ephemeral messages
(correspondent replies, RECALL) must survive a transient LLM failure.

get_messages_for_llm(consume=True) now MOVES ephemerals to a pending buffer instead of
dropping them; commit drops them once the LLM responded, restore re-queues them on
failure. Correspondent data has no other delivery path, so losing it is a real bug.
"""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import HumanMessage


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="t", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=8000, session_id="s-eph",
    )


def _texts(msgs):
    return [m.content if isinstance(m.content, str) else str(m.content) for m in msgs]


def test_ephemeral_included_then_committed_on_success():
    mm = _mm()
    mm.push_ephemeral_message(HumanMessage(content="EPHEMERAL_ONE_SHOT"))
    msgs = mm.get_messages_for_llm(consume_ephemeral=True)
    assert any("EPHEMERAL_ONE_SHOT" in t for t in _texts(msgs))
    # success -> commit -> not re-included next assembly
    mm.commit_ephemeral_consumption()
    again = mm.get_messages_for_llm(consume_ephemeral=True)
    assert not any("EPHEMERAL_ONE_SHOT" in t for t in _texts(again))


def test_ephemeral_restored_on_failure():
    mm = _mm()
    mm.push_ephemeral_message(HumanMessage(content="CORRESPONDENT_REPLY"))
    msgs = mm.get_messages_for_llm(consume_ephemeral=True)
    assert any("CORRESPONDENT_REPLY" in t for t in _texts(msgs))
    # transient LLM failure -> restore -> re-included on the NEXT assembly
    mm.restore_ephemeral_on_failure()
    again = mm.get_messages_for_llm(consume_ephemeral=True)
    assert any("CORRESPONDENT_REPLY" in t for t in _texts(again)), \
        "a consumed ephemeral must be re-queued when the LLM call failed"


def test_restore_is_noop_after_commit():
    """A committed (delivered) ephemeral must NOT be resurrected by a later restore."""
    mm = _mm()
    mm.push_ephemeral_message(HumanMessage(content="DELIVERED_ONCE"))
    mm.get_messages_for_llm(consume_ephemeral=True)
    mm.commit_ephemeral_consumption()
    mm.restore_ephemeral_on_failure()  # must be a no-op (pending already cleared)
    again = mm.get_messages_for_llm(consume_ephemeral=True)
    assert not any("DELIVERED_ONCE" in t for t in _texts(again))
