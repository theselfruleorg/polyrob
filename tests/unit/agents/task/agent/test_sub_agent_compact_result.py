"""102 M-E: the manager keeps a compact record; the parent gets the full result."""
from agents.task.agent.sub_agent_manager import (
    COMPACT_RESULT_CHARS, SubAgentOutput, SubAgentResult, _compact_result,
)


def test_the_stored_record_is_short_and_keeps_the_stats():
    full = SubAgentResult(agent_id="a", task="t" * 9000, success=True, steps_taken=4,
                          output=SubAgentOutput(final_text="r" * 90000), finished=True)
    kept = _compact_result(full)
    assert isinstance(kept.output, str) and len(kept.output) <= COMPACT_RESULT_CHARS
    assert kept.output.startswith("**Report:**")
    assert len(kept.task) == COMPACT_RESULT_CHARS
    assert (kept.success, kept.steps_taken, kept.finished) == (True, 4, True)
    assert full.output.final_text == "r" * 90000, "the caller's result is not touched"


def test_a_failed_child_with_no_output_is_safe():
    kept = _compact_result(SubAgentResult(agent_id="a", task="t", output="", success=False, error="no"))
    assert kept.output == "" and kept.error == "no"
