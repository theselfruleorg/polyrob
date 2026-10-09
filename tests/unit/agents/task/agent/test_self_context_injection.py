"""polyrob Phase C: SOUL/IDENTITY self-context is pinned as a frozen foundation
user message (like skills), NOT embedded in the system prompt — so the system
prompt stays stable/cacheable and the identity reads as a distinct block.
"""
from unittest.mock import MagicMock
import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import MessageOrigin, make_control_message


def _mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
    )


def _text(m):
    return m.content if isinstance(m.content, str) else str(m.content)


def test_self_context_injected_into_llm_messages_not_system_prompt():
    mm = _mm()
    mm.set_self_context_message("I am ROB, a careful operator.")
    msgs = mm.get_messages_for_llm()

    # System prompt must NOT contain the SOUL content (kept stable/cacheable).
    assert "I am ROB, a careful operator." not in _text(msgs[0])

    blocks = [m for m in msgs if "self-context" in _text(m)]
    assert len(blocks) == 1
    assert "I am ROB, a careful operator." in _text(blocks[0])
    assert blocks[0].to_dict()["role"] == "user"


def test_make_control_message_self_context_origin():
    m = make_control_message("soul here", MessageOrigin.SELF_CONTEXT)
    assert m.origin == MessageOrigin.SELF_CONTEXT
    assert "<self-context>" in m.content


def test_no_self_context_block_when_unset():
    mm = _mm()
    msgs = mm.get_messages_for_llm()
    assert not [m for m in msgs if "self-context" in _text(m)]


def test_set_empty_self_context_is_noop():
    mm = _mm()
    mm.set_self_context_message("")
    msgs = mm.get_messages_for_llm()
    assert not [m for m in msgs if "self-context" in _text(m)]


def test_self_context_tokens_counted_in_foundation():
    mm = _mm()
    before = mm.get_total_tokens() if hasattr(mm, "get_total_tokens") else None
    mm.set_self_context_message("I am ROB. " * 50)
    # token cost is tracked so compaction/overflow math sees it
    assert getattr(mm, "_self_context_tokens", 0) > 0


def test_self_context_precedes_skills_in_foundation():
    mm = _mm()
    mm.set_self_context_message("SOUL_MARKER")
    mm.set_skill_message("SKILL_MARKER")
    msgs = mm.get_messages_for_llm()
    joined = [_text(m) for m in msgs]
    soul_idx = next(i for i, t in enumerate(joined) if "SOUL_MARKER" in t)
    skill_idx = next(i for i, t in enumerate(joined) if "SKILL_MARKER" in t)
    assert soul_idx < skill_idx


def test_self_context_loaded_from_disk_appears_in_foundation(tmp_path):
    """End-to-end contract the construction glue relies on: operator-authored docs
    on disk -> load_self_context -> set_self_context_message -> foundation block."""
    from core.instance import load_self_context

    idir = tmp_path / "identity"
    idir.mkdir()
    (idir / "identity.md").write_text("I am ROB, operator-authored.")

    mm = _mm()
    mm.set_self_context_message(load_self_context(tmp_path))
    msgs = mm.get_messages_for_llm()
    blocks = [m for m in msgs if "self-context" in _text(m)]
    assert len(blocks) == 1
    assert "I am ROB, operator-authored." in _text(blocks[0])


@pytest.mark.parametrize("combined", ["true", "false"])
def test_live_correspondent_turn_hides_already_pinned_owner_documents(monkeypatch, combined):
    from types import SimpleNamespace
    from agents.task.session_class import may_write_owner_memory
    from agents.task.agent.messages.foundation_replay import capture_foundation, replay_foundation
    monkeypatch.setenv("SELF_CONTEXT_COMBINED", combined)
    mm = _mm()
    orch = SimpleNamespace(_correspondent_tainted=False, _public_session=False)
    mm._owner_context_allowed = lambda: may_write_owner_memory(orch)
    mm.set_self_context_blocks((("owner_rules", "OWNER_PRIVATE"), ("self_doc", "SELF_PRIVATE")))
    mm.set_project_context_message("PROJECT_PRIVATE")
    saved = capture_foundation(mm)
    assert "OWNER_PRIVATE" in "\n".join(_text(m) for m in mm.get_messages_for_llm())

    orch._correspondent_tainted = True
    for messages in (mm.get_messages(), mm.get_messages_for_llm()):
        text = "\n".join(_text(m) for m in messages)
        assert "OWNER_PRIVATE" not in text and "SELF_PRIVATE" not in text and "PROJECT_PRIVATE" not in text
    hidden = capture_foundation(mm)
    assert hidden["self_context"] is hidden["self_context_blocks"] is hidden["project_context"] is None
    assert replay_foundation(mm, saved)
    assert mm._self_context_message is mm._self_context_blocks is mm._project_context_message is None


def test_owner_document_guard_probe_failure_is_closed():
    mm = _mm()
    mm.set_self_context_message("OWNER_PRIVATE")
    def broken():
        raise OSError("session state unreadable")
    mm._owner_context_allowed = broken
    assert "OWNER_PRIVATE" not in "\n".join(_text(m) for m in mm.get_messages_for_llm())
