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


def test_status_label_names_the_typed_stop_reason():
    """083 WS-1: no fixed "step limit" guess — the child's real stop reason."""
    r = SubAgentResult(agent_id="c1", task="t", output="x", success=True, finished=False,
                       steps_taken=4, max_steps=30, stop_reason="failures")
    assert r.status_label == ("Stopped before finishing (no done — stopped after too "
                              "many consecutive failed steps (4/30))")


def test_priority_one_reads_the_real_ledger():
    """083 B7: ``_extract_output_structured`` read ``agent.state.last_result``,
    which nothing sets; the ledger is ``agent._last_result``."""
    from types import SimpleNamespace
    from agents.task.agent.views import ActionResult
    mgr = object.__new__(SubAgentManager)
    mgr.logger = __import__("logging").getLogger("t")
    mgr.orchestrator = SimpleNamespace(_path_manager=None)
    agent = SimpleNamespace(state=SimpleNamespace(), task="t",
                            _last_result=[ActionResult(extracted_content="the child's data")])
    out = mgr._extract_output_structured(agent, None)
    assert out.extracted_content == ["the child's data"]


def test_a_stopped_child_carries_its_last_report_once():
    """083 WS-2: the child's last report becomes its labelled final text, not a
    duplicate extracted item."""
    from types import SimpleNamespace
    from agents.task.agent.views import ActionResult
    mgr = object.__new__(SubAgentManager)
    mgr.logger = __import__("logging").getLogger("t")
    mgr.orchestrator = SimpleNamespace(_path_manager=None)
    item = ActionResult(extracted_content="half done", metadata={"exhausted_summary": True})
    agent = SimpleNamespace(state=SimpleNamespace(exhausted_summary="half done"), task="t",
                            _last_result=[item])
    out = mgr._extract_output_structured(agent, None)
    assert out.final_text == "Last report (agent's words): half done"
    assert "half done" not in out.extracted_content


def test_a_failed_child_with_a_typed_stop_names_it():
    """083: a child cut by its wall-clock cap says so, not a bare "Failed"."""
    r = SubAgentResult(agent_id="c1", task="t", output="", success=False,
                       stop_reason="wall_clock", steps_taken=3)
    assert r.status_label == "Failed (cut by the wall-clock cap (step 3))"


def test_an_unfinished_child_summary_does_not_say_completed():
    """083: only a child that called done() gets "Completed task: …"."""
    from types import SimpleNamespace
    mgr = object.__new__(SubAgentManager)
    mgr.logger = __import__("logging").getLogger("t")
    mgr.orchestrator = SimpleNamespace(_path_manager=None)
    stopped = SimpleNamespace(state=SimpleNamespace(stop_reason="max_steps"), task="t")
    done = SimpleNamespace(state=SimpleNamespace(stop_reason="done"), task="t")
    assert mgr._extract_output_structured(stopped, None).summary == "Task: t"
    assert mgr._extract_output_structured(done, None).summary == "Completed task: t"
