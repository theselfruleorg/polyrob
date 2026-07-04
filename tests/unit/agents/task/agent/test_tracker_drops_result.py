"""M2: ToolCallTracker.mark_completed stored the FULL untruncated tool result on the
completed record and kept it in _completed_calls for the whole session (reset() has no
callers). Nothing ever reads record.result back, so on a long autonomous run this is an
ever-growing duplicate of every large tool output. Drop the payload on completion.
"""
from agents.task.agent.tool_call_tracker import ToolCallTracker


def test_completed_call_drops_full_result_payload():
    t = ToolCallTracker(session_id="s1")
    t.register_tool_calls([{"id": "c1", "name": "read_file", "args": {}}])
    big = "x" * 500_000

    assert t.mark_completed("c1", result=big) is True

    rec = t._completed_calls["c1"]
    assert rec.status == "completed"
    assert rec.result is None  # payload not retained for the session lifetime
    # name/status are preserved (that's all get_statistics/has_call need).
    assert rec.name == "read_file"
