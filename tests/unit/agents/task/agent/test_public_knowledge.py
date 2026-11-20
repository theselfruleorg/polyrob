"""The operator's public knowledge (identity/public.md): the one instance text that a
public room and a correspondent session also get, while every owner document stays out."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.session_class import may_write_owner_memory
from core.instance import PUBLIC_DOC_MAX_CHARS, load_public_doc
from modules.llm.messages import MessageOrigin


def _mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(llm=llm, task="Test task", action_descriptions="acts",
                          system_prompt_class=SystemPrompt, max_input_tokens=4000)


def _all_text(mm) -> str:
    return "\n".join(m.content if isinstance(m.content, str) else str(m.content)
                     for m in mm.get_messages_for_llm())


def test_load_public_doc_absent_blank_present(tmp_path):
    assert load_public_doc(tmp_path) == ""
    (tmp_path / "identity").mkdir()
    (tmp_path / "identity" / "public.md").write_text("   \n", encoding="utf-8")
    assert load_public_doc(tmp_path) == ""
    (tmp_path / "identity" / "public.md").write_text("The agent runs on POLYROB.", encoding="utf-8")
    out = load_public_doc(tmp_path)
    assert out.startswith("## Public knowledge") and "The agent runs on POLYROB." in out


def test_load_public_doc_over_cap_keeps_the_head_under_a_banner(tmp_path):
    (tmp_path / "identity").mkdir()
    (tmp_path / "identity" / "public.md").write_text("A" * (PUBLIC_DOC_MAX_CHARS + 500), encoding="utf-8")
    out = load_public_doc(tmp_path)
    assert "TRUNCATED" in out and out.splitlines()[-1] == "A" * PUBLIC_DOC_MAX_CHARS


def test_public_knowledge_is_its_own_enveloped_row():
    mm = _mm()
    mm.set_public_knowledge("FACTS_FOR_ANYONE")
    rows = [m for m in mm.get_messages_for_llm() if "FACTS_FOR_ANYONE" in str(m.content)]
    assert len(rows) == 1 and rows[0].origin == MessageOrigin.PUBLIC_KNOWLEDGE
    assert "<public-knowledge>" in rows[0].content
    mm.set_public_knowledge("")
    assert "FACTS_FOR_ANYONE" not in _all_text(mm)


def test_a_room_or_correspondent_turn_sees_public_knowledge_and_no_owner_document():
    mm = _mm()
    orch = SimpleNamespace(_correspondent_tainted=False, _public_session=False)
    mm._owner_context_allowed = lambda: may_write_owner_memory(orch)
    mm.set_self_context_blocks((("soul", "SOUL_PRIVATE"), ("owner_rules", "OWNER_PRIVATE")))
    mm.set_public_knowledge("FACTS_FOR_ANYONE")
    assert "SOUL_PRIVATE" in _all_text(mm) and "FACTS_FOR_ANYONE" in _all_text(mm)

    for flag in ("_public_session", "_correspondent_tainted"):
        setattr(orch, flag, True)
        text = _all_text(mm)
        assert "FACTS_FOR_ANYONE" in text, flag
        assert "SOUL_PRIVATE" not in text and "OWNER_PRIVATE" not in text, flag
        setattr(orch, flag, False)
