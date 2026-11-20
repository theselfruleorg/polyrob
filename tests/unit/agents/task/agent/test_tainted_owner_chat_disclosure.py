"""An owner chat that a correspondent's reply taints (owner 2026-11-21, option A).

The agent emailed a person from the owner's chat; their reply comes back into it.
Until the owner writes again, the chat must not show the model line, the
<environment> block or the chat's H-MEM summary, and the file ability is refused
there (the scoped reply could carry the bytes out). A correspondent's OWN session
keeps its workspace.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.core.correspondent_gate import make_correspondent_gate_hook
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt


def _mm(visible):
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    mm = MessageManager(llm=llm, task="t", action_descriptions="acts",
                        system_prompt_class=SystemPrompt, max_input_tokens=4000)
    mm._owner_context_allowed = lambda: visible["on"]
    mm._owner_chat_tainted = lambda: not visible["on"]
    mm.set_runtime_identity("secret-model", "secret-provider")
    mm.set_environment_message("<environment>host-xyz /home/owner/project</environment>")
    return mm


def _text(mm):
    return "\n".join(m.content if isinstance(m.content, str) else str(m.content)
                     for m in mm.get_messages_for_llm())


def test_the_owner_chat_shows_the_model_and_environment():
    text = _text(_mm({"on": True}))
    assert "secret-model" in text and "host-xyz" in text


def test_a_tainted_owner_chat_hides_them_live_and_shows_them_again():
    visible = {"on": False}
    mm = _mm(visible)
    text = _text(mm)
    assert "secret-model" not in text and "secret-provider" not in text
    assert "host-xyz" not in text
    assert "You are POLYROB" in text            # the product line stays
    visible["on"] = True                         # the owner writes again
    text = _text(mm)
    assert "secret-model" in text and "host-xyz" in text


def test_a_tainted_owner_chat_gets_no_hmem_summary():
    visible = {"on": False}
    mm = _mm(visible)
    tcm = MagicMock()
    tcm.get_context_injection.return_value = "PRIVATE-HMEM-SUMMARY"
    mm.task_context_manager = tcm
    mm.session_id = "s1"
    assert "PRIVATE-HMEM-SUMMARY" not in _text(mm)
    visible["on"] = True
    assert "PRIVATE-HMEM-SUMMARY" in _text(mm)


def test_self_facts_after_a_swap_name_no_model_while_tainted():
    from agents.task.agent.core.state_delta import self_facts_text
    mm = _mm({"on": False})
    agent = SimpleNamespace(model_name="secret-model", llm_provider="secret-provider",
                            message_manager=mm, use_vision=False)
    assert "secret-model" not in self_facts_text(agent, "owner", "old-model")


def _gate(owner_chat, tainted=True):
    return make_correspondent_gate_hook(
        lambda: tainted, resolve_tool=lambda name: "filesystem",
        owner_chat=lambda: owner_chat)


def test_a_tainted_owner_chat_refuses_the_file_ability():
    assert _gate(owner_chat=True)("read_file", {"path": "x"}, None)
    assert _gate(owner_chat=True)("write_file", {"path": "x"}, None)


def test_a_correspondents_own_session_keeps_its_workspace():
    assert _gate(owner_chat=False)("read_file", {"path": "x"}, None) is None


def test_an_untainted_owner_chat_keeps_the_file_ability():
    assert _gate(owner_chat=True, tainted=False)("read_file", {"path": "x"}, None) is None


def test_a_broken_owner_probe_fails_closed():
    def boom():
        raise RuntimeError("probe")
    hook = make_correspondent_gate_hook(lambda: True, resolve_tool=lambda n: "filesystem",
                                        owner_chat=boom)
    assert hook("read_file", {}, None)


def test_owner_workspace_is_the_file_ability_only():
    from core.tool_capabilities import ids_with
    assert ids_with("owner_workspace") == frozenset({"filesystem"})


def test_owner_chat_is_not_a_correspondent_session_or_an_autonomous_run(monkeypatch):
    from agents.task.agent.core import correspondent_gate as cg
    import core.autonomy_marker as am
    assert cg._is_owner_chat(SimpleNamespace(session_id="chat-1")) is True
    assert cg._is_owner_chat(SimpleNamespace(session_id="c", _correspondent_session=True)) is False
    monkeypatch.setattr(am, "is_autonomous", lambda sid: sid == "goal-run")
    assert cg._is_owner_chat(SimpleNamespace(session_id="goal-run")) is False


def test_a_room_or_correspondent_session_keeps_its_own_hmem():
    """Owner documents hidden for good (a room) is not an owner chat a reply taints."""
    mm = _mm({"on": True})
    mm._owner_context_allowed = lambda: False      # a room: no owner documents
    mm._owner_chat_tainted = lambda: False
    tcm = MagicMock()
    tcm.get_context_injection.return_value = "ROOM-OWN-SUMMARY"
    mm.task_context_manager = tcm
    mm.session_id = "room-1"
    assert "ROOM-OWN-SUMMARY" in _text(mm)


def test_the_workspace_row_hides_only_in_a_tainted_owner_chat():
    visible = {"on": False}
    mm = _mm(visible)
    mm.set_workspace_message("<workspace>/home/owner/secret-project</workspace>")
    assert "secret-project" not in _text(mm)
    visible["on"] = True
    assert "secret-project" in _text(mm)


def test_a_restored_tainted_chat_keeps_environment_for_the_owners_next_turn():
    """Replay while tainted must not clear a row the live guard only hides."""
    from agents.task.agent.messages.foundation_replay import capture_foundation, replay_foundation
    visible = {"on": True}
    mm = _mm(visible)
    blob = capture_foundation(mm)
    visible["on"] = False                          # restored with the taint on
    mm2 = _mm(visible)
    replay_foundation(mm2, blob)
    assert "host-xyz" not in _text(mm2)
    visible["on"] = True                           # the owner writes
    assert "host-xyz" in _text(mm2)


def test_a_snapshot_taken_while_tainted_keeps_the_hidden_rows():
    from agents.task.agent.messages.foundation_replay import capture_foundation
    mm = _mm({"on": False})
    assert "host-xyz" in (capture_foundation(mm).get("environment") or "")


def test_the_token_gauge_skips_hidden_rows():
    from agents.task.agent.messages.token_counter import _foundation_tokens
    visible = {"on": True}
    mm = _mm(visible)
    shown = _foundation_tokens(mm)
    visible["on"] = False
    assert _foundation_tokens(mm) < shown


def test_a_reply_with_a_file_is_not_a_clean_reply():
    from agents.task.agent.core.correspondent_gate import _reply_target
    assert _reply_target("message", {"surface": "email", "target": "a@x.test",
                                     "media_paths": ["notes.txt"]}) == ("", "")
    assert _reply_target("message", {"surface": "email", "target": "a@x.test"}) \
        == ("email", "a@x.test")


def test_an_unresolvable_action_fails_closed_in_a_tainted_owner_chat():
    def boom(name):
        raise RuntimeError("registry")
    hook = make_correspondent_gate_hook(lambda: True, resolve_tool=boom, owner_chat=lambda: True)
    assert hook("read_file", {}, None)


def test_owner_documents_survive_a_restore_while_the_owner_chat_is_tainted():
    from agents.task.agent.messages.foundation_replay import capture_foundation, replay_foundation
    visible = {"on": True}
    mm = _mm(visible)
    mm.set_self_context_blocks((("owner_rules", "OWNER_RULES_TEXT"),))
    blob = capture_foundation(mm)
    visible["on"] = False
    mm2 = _mm(visible)
    mm2.set_self_context_blocks((("owner_rules", "OWNER_RULES_TEXT"),))
    replay_foundation(mm2, blob)
    assert "OWNER_RULES_TEXT" not in _text(mm2)
    visible["on"] = True
    assert "OWNER_RULES_TEXT" in _text(mm2)
