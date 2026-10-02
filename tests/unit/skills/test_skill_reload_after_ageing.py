"""Review 2026-09-29 D4: a skill body the context lost can be loaded again.

Before: ageing (or compaction) cut the ``load_skill`` result down to one line,
and every later ``load_skill`` for that id answered "already active — no need
to reload". The skill body was gone for the rest of the session.
"""
from types import SimpleNamespace

from modules.llm.messages import AIMessage, HumanMessage, ToolMessage

from agents.task.agent.messages.filters import age_old_tool_results
from tools.controller._helpers import build_load_skill_result, forget_activated_skill

BODY = "Step 1: resolve the address. " * 200  # ~5.8k chars, over the age limit


def _history(skill_msg_content):
    msgs = [HumanMessage(content="task"),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "load_skill",
                                               "args": {"skill_id": "token-identity"}}]),
            ToolMessage(content=skill_msg_content, tool_call_id="c1")]
    msgs += [HumanMessage(content=f"turn {i}") for i in range(10)]
    return msgs


def test_a_load_skill_result_is_never_aged():
    loaded = f'<skill id="token-identity">\n{BODY}\n</skill>'
    aged = age_old_tool_results(_history(loaded), keep_recent=2, max_chars=2000)
    assert aged[2].content == loaded


def test_other_long_tool_results_still_age():
    aged = age_old_tool_results(_history("x" * 5000), keep_recent=2, max_chars=2000)
    assert len(aged[2].content) < 1000


def test_a_repeat_after_the_ack_resends_the_body():
    skills = {"token-identity": SimpleNamespace(content=BODY)}
    activated = set()
    first = build_load_skill_result(skills, "token-identity", activated=activated)
    assert BODY in first.extracted_content
    second = build_load_skill_result(skills, "token-identity", activated=activated)
    assert second.metadata.get("skill_already_active") is True
    assert BODY not in second.extracted_content
    assert "once more" in second.extracted_content
    # The body was compacted away; the model follows the ack's instruction.
    third = build_load_skill_result(skills, "token-identity", activated=activated)
    assert BODY in third.extracted_content
    assert third.metadata.get("skill_loaded") == "token-identity"
    # And the cycle starts again: the next repeat acks.
    fourth = build_load_skill_result(skills, "token-identity", activated=activated)
    assert fourth.metadata.get("skill_already_active") is True


def test_forget_clears_the_ack_marker_too():
    skills = {"s": SimpleNamespace(content="body")}
    activated = set()
    build_load_skill_result(skills, "s", activated=activated)
    build_load_skill_result(skills, "s", activated=activated)
    ctrl = SimpleNamespace(_activated_skills=activated)
    forget_activated_skill(ctrl, "s")
    assert activated == set()
