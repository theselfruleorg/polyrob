"""A child that hit its step limit without calling done is not "Completed".

``success`` only said the run returned without an exception (harness review
G5); ``finished`` says the child called done.
"""
from agents.task.agent.sub_agent_manager import SubAgentManager, SubAgentResult


def _r(success=True, finished=None):
    return SubAgentResult(agent_id="c1", task="t", output="partial notes",
                          success=success, finished=finished)


def test_status_label():
    assert _r(finished=True).status_label == "Completed"
    assert _r(finished=None).status_label == "Completed"  # unknown keeps the old reading
    assert _r(finished=False).status_label.startswith("Stopped before finishing")
    assert _r(success=False, finished=False).status_label == "Failed"


def test_prompt_rendering_names_a_stopped_child():
    mgr = object.__new__(SubAgentManager)
    text = mgr.format_results_for_prompt([_r(finished=True), _r(finished=False)])
    assert "## Subtask 1 ✅" in text and "## Subtask 2 ⚠️" in text
    assert "**Status:** Stopped before finishing" in text
