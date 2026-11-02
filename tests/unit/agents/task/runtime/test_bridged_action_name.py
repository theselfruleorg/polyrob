"""A late tool called through the ``tool_call`` bridge is recorded in the ledger
under the OUTER verb. Every ledger reader must see the inner action name
(prod 2026-10-29: the ship==tested gate never saw a bridged green
``coding_run_tests``, and a bridged ``coding_str_replace`` after it was
invisible to ``edited_since_last_test``)."""
import types

from agents.task.runtime.edit_verify import edited_since_last_test
from agents.task.runtime.run_outcome import _action_name
from tools.hf_deploy import digest as _digest


class _Action:
    def __init__(self, dump):
        self._dump = dump

    def model_dump(self, exclude_unset=True):
        return self._dump


def _direct(name):
    return _Action({name: {}})


def _bridged(name):
    return _Action({"tool_call": {"name": name, "arguments": {}}})


def _orch(entries):
    agent = types.SimpleNamespace(_is_sub_agent=False)
    steps = []
    for action, error in entries:
        result = types.SimpleNamespace(error=error, extracted_content="ok")
        steps.append(types.SimpleNamespace(
            model_output=types.SimpleNamespace(action=[action]), result=[result]))
    agent.history = types.SimpleNamespace(history=steps)
    return types.SimpleNamespace(agents={"a1": agent}, session_id=None)


def test_action_name_resolves_the_bridged_target():
    assert _action_name(_bridged("coding_run_tests")) == "coding_run_tests"
    assert _action_name(_direct("coding_run_tests")) == "coding_run_tests"


def test_action_name_keeps_tool_call_without_a_target():
    assert _action_name(_Action({"tool_call": {"arguments": {}}})) == "tool_call"
    assert _action_name(_Action({"tool_call": {"name": "  "}})) == "tool_call"


def test_bridged_green_test_allows_deploy(tmp_path):
    (tmp_path / "app.py").write_text("print(1)\n")
    orch = _orch([(_bridged("coding_str_replace"), None),
                  (_bridged("coding_run_tests"), None)])
    digest, reason = _digest.tested_tree_digest(orch, str(tmp_path))
    assert reason is None and digest


def test_bridged_edit_after_green_test_refuses_deploy(tmp_path):
    (tmp_path / "app.py").write_text("print(1)\n")
    orch = _orch([(_direct("coding_run_tests"), None),
                  (_bridged("coding_str_replace"), None)])
    assert edited_since_last_test(orch) is True
    digest, reason = _digest.tested_tree_digest(orch, str(tmp_path))
    assert digest is None and "edited after the last green run_tests" in reason
