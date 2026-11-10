"""103 D3: what the owner says in a clean turn applies at once, and only then."""
from types import SimpleNamespace

import pytest

from modules.llm.messages import HumanMessage, make_control_message
from tools.controller import doc_authoring as D


def _controller(*messages):
	history = SimpleNamespace(messages=[SimpleNamespace(message=m) for m in messages])
	agent = SimpleNamespace(message_manager=SimpleNamespace(history=history))
	return SimpleNamespace(orchestrator=SimpleNamespace(agents={"a": agent}))


def _params(action="patch", new="- Do not ask before a results post."):
	return SimpleNamespace(action=action, old_string="- an old rule", new_string=new)


@pytest.fixture
def genuine(monkeypatch):
	import core.security.owner_turn as O
	import core.security.taints as T
	monkeypatch.setattr(O, "is_genuine_owner_turn", lambda *a, **k: True)
	monkeypatch.setattr(T, "turn_tainted", lambda *a, **k: False)


def test_a_clean_owner_turn_applies_at_once(genuine):
	assert D._owner_doc_applies_now(_controller(HumanMessage(content="stop asking")), _params(), object())


def test_external_tool_output_since_the_owners_message_makes_a_draft(genuine):
	from modules.llm.messages import ToolMessage
	tool = ToolMessage(content='<untrusted_tool_result source="web">x</untrusted_tool_result>',
	                   tool_call_id="c1")
	assert not D._owner_doc_applies_now(
		_controller(HumanMessage(content="stop asking"), tool), _params(), object())


def test_external_output_before_the_owners_message_does_not_count(genuine):
	from modules.llm.messages import ToolMessage
	tool = ToolMessage(content='<untrusted_tool_result source="web">x</untrusted_tool_result>',
	                   tool_call_id="c1")
	assert D._owner_doc_applies_now(
		_controller(tool, HumanMessage(content="stop asking")), _params(), object())


def test_unreadable_history_makes_a_draft(genuine):
	assert not D._owner_doc_applies_now(SimpleNamespace(orchestrator=None), _params(), object())


def test_a_whole_document_or_a_long_rule_applies_on_a_clean_turn(genuine):
	ctl = _controller(HumanMessage(content="x"))
	assert D._owner_doc_applies_now(ctl, _params(action="update"), object())
	assert D._owner_doc_applies_now(ctl, _params(new="y" * 5000), object())


def test_a_forged_or_tainted_turn_is_a_draft(monkeypatch):
	import core.security.owner_turn as O
	import core.security.taints as T
	ctl = _controller(HumanMessage(content="x"))
	monkeypatch.setattr(O, "is_genuine_owner_turn", lambda *a, **k: False)
	monkeypatch.setattr(T, "turn_tainted", lambda *a, **k: False)
	assert not D._owner_doc_applies_now(ctl, _params(), object())
	monkeypatch.setattr(O, "is_genuine_owner_turn", lambda *a, **k: True)
	monkeypatch.setattr(T, "turn_tainted", lambda *a, **k: True)
	assert not D._owner_doc_applies_now(ctl, _params(), object())


def test_a_runtime_note_is_not_the_owners_message(genuine):
	from modules.llm.messages import MessageOrigin, ToolMessage
	tool = ToolMessage(content='<untrusted_tool_result source="web">x</untrusted_tool_result>',
	                   tool_call_id="c1")
	note = make_control_message("a nudge", MessageOrigin.SYSTEM_NOTE)
	assert not D._owner_doc_applies_now(
		_controller(HumanMessage(content="x"), tool, note), _params(), object())


# --- with the real writer ---------------------------------------------------------
@pytest.fixture
def writer(tmp_path):
	from core.owner_doc_writer import OwnerDocWriter
	w = OwnerDocWriter(str(tmp_path), instance_id="i")
	assert w.propose("# Owner\n\n- rule A\n- rule B\n", user_id="u1", created_by="user", pending=False).ok
	return w


def _patch(old, new):
	return SimpleNamespace(action="patch", old_string=old, new_string=new, replace_all=False,
	                       source=None, observed_at=None)


def test_a_rule_applied_at_once_never_activates_an_unreviewed_draft(writer):
	draft = _patch("- rule B", "- rule B\n- a draft line nobody reviewed")
	assert writer.patch(user_id="u1", old_string=draft.old_string, new_string=draft.new_string,
	                    created_by="agent", pending=True).pending
	res, err, now = D._write_owner_rule(writer, _patch("- rule A", "- rule C"),
	                                    user_id="u1", created_by="agent", now=True)
	assert (res.ok, res.pending, err, now) == (True, False, None, True)
	active = writer.read_active("u1")
	assert "- rule C" in active and "nobody reviewed" not in active


def test_promoting_the_older_draft_keeps_the_rule_applied_at_once(writer):
	writer.patch(user_id="u1", old_string="- rule B", new_string="- rule B\n- a draft line",
	             created_by="agent", pending=True)
	D._write_owner_rule(writer, _patch("- rule A", "- rule C"), user_id="u1",
	                    created_by="agent", now=True)
	assert writer.promote(user_id="u1").ok
	editable = writer.read_active("u1").split("## Superseded")[0]
	assert "- rule C" in editable and "- a draft line" in editable and "- rule A" not in editable


def test_a_rule_only_the_draft_holds_is_edited_in_the_draft(writer):
	writer.patch(user_id="u1", old_string="- rule B", new_string="- rule B\n- drafted",
	             created_by="agent", pending=True)
	res, _, now = D._write_owner_rule(writer, _patch("- drafted", "- drafted better"),
	                                  user_id="u1", created_by="agent", now=True)
	assert res.ok and res.pending and now is False
	assert "drafted" not in writer.read_active("u1")


def test_the_owner_undoes_the_rule_with_no_tap_and_its_text_is_kept(genuine, writer):
	params = _patch("- rule A", "")
	assert D._owner_doc_applies_now(_controller(HumanMessage(content="undo it")), params, object())
	res, _, now = D._write_owner_rule(writer, params, user_id="u1", created_by="agent", now=True)
	assert res.ok and not res.pending and now
	active = writer.read_active("u1")
	editable, superseded = active.split("## Superseded")
	assert "rule A" not in editable and "rule A" in superseded


def test_an_untrusted_result_in_any_agent_of_the_session_makes_a_draft(genuine):
	from modules.llm.messages import ToolMessage
	tool = ToolMessage(content='<untrusted_tool_result source="web">x</untrusted_tool_result>',
	                   tool_call_id="c1")
	first = _controller(HumanMessage(content="x")).orchestrator.agents["a"]
	second = _controller(HumanMessage(content="x"), tool).orchestrator.agents["a"]
	ctl = SimpleNamespace(orchestrator=SimpleNamespace(agents={"a": first, "b": second}))
	assert not D._owner_doc_applies_now(ctl, _params(), object())


def test_a_result_that_compaction_removed_still_makes_a_draft(genuine):
	controller = _controller(HumanMessage(content="stop asking"))
	controller.orchestrator.agents["a"].message_manager._untrusted_since_owner = True
	assert not D._owner_doc_applies_now(controller, _params(), object())


def test_a_page_the_session_used_makes_a_draft(genuine):
	controller = _controller(HumanMessage(content="stop asking"))
	controller.orchestrator.agents["a"]._browser_page_used = True
	assert not D._owner_doc_applies_now(controller, _params(), object())


def test_the_first_message_of_a_session_applies_at_once(genuine):
	controller = _controller()
	controller.orchestrator.agents["a"].message_manager._initial_task_message = object()
	assert D._owner_doc_applies_now(controller, _params(), object())


def test_the_latch_is_set_on_add_and_cleared_by_the_owner_drain():
	from agents.task.agent.core.user_ingress import _update_forged_turn_marker
	from modules.llm.messages import ToolMessage
	from tests.unit.agents.test_checkpoint_fidelity import _mm
	manager = _mm()
	manager._add_message_with_tokens(
		ToolMessage(content='<untrusted_tool_result source="web">x</untrusted_tool_result>',
		            tool_call_id="c1"), _internal=True)
	assert manager._untrusted_since_owner is True
	orch = SimpleNamespace(agents={"a": SimpleNamespace(message_manager=manager)},
	                       _clear_correspondent_taint=lambda: None)
	_update_forged_turn_marker(orch, [{"kind": "comment", "text": "hi", "metadata": {"typed_text": "hi"}}])
	assert manager._untrusted_since_owner is False


def test_the_owner_turn_is_not_clean_on_a_standing_job(monkeypatch):
	import core.security.owner_turn as O
	monkeypatch.setattr(O, "is_owner_standing_run", lambda ctx: True)
	assert not D._owner_turn_is_clean(_controller(HumanMessage(content="x")), object())
