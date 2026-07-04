"""Regression test for tool-call <-> result misalignment under action reordering.

Fusion review 2026-06-14, finding C2/#2: when actions are reordered (MCP vs
non-MCP) the positional pairing in _add_tool_messages attaches a result to the
wrong tool_call_id.
"""
import pytest
from tools.controller.registry.views import ActionModel
from tools.controller.types import ActionResult
from agents.task.agent.core.result_processing import _pair_results_to_calls


def test_action_model_carries_tool_call_id():
    a = ActionModel()
    a._tool_call_id = "call_A"
    assert a._tool_call_id == "call_A"


def test_action_result_carries_source_tool_call_id():
    r = ActionResult(extracted_content="hi")
    r.tool_call_id = "call_A"
    assert r.tool_call_id == "call_A"


def test_results_pair_by_identity_after_reorder():
    tool_calls_to_pass = [
        {"id": "call_A", "name": "read_file"},
        {"id": "call_B", "name": "mcp_search"},
    ]
    r_b = ActionResult(extracted_content="B-OUTPUT"); r_b.tool_call_id = "call_B"
    r_a = ActionResult(extracted_content="A-OUTPUT"); r_a.tool_call_id = "call_A"
    paired = _pair_results_to_calls([r_b, r_a], tool_calls_to_pass)
    assert paired["call_A"][0] == "A-OUTPUT"
    assert paired["call_B"][0] == "B-OUTPUT"


def test_duplicate_tool_call_id_does_not_drop_a_result():
    """UP-01 Item 2 / B25: two results sharing one tool_call_id must NOT silently
    clobber. The dup-id fallback now keys by (tool_call_id, position) so BOTH results
    surface even when the call ids themselves collide; the consumer looks up by
    (id, i) then by id.
    """
    tool_calls_to_pass = [
        {"id": "call_A", "name": "read_file"},
        {"id": "call_B", "name": "read_file"},
    ]
    # Provider erroneously emits the SAME id on both results.
    r1 = ActionResult(extracted_content="FIRST"); r1.tool_call_id = "dup"
    r2 = ActionResult(extracted_content="SECOND"); r2.tool_call_id = "dup"
    paired = _pair_results_to_calls([r1, r2], tool_calls_to_pass)
    # Neither result is lost: position-keyed fallback.
    contents = {v[0] for v in paired.values()}
    assert contents == {"FIRST", "SECOND"}
    assert paired[("call_A", 0)][0] == "FIRST"
    assert paired[("call_B", 1)][0] == "SECOND"


def test_duplicate_call_ids_both_surface_to_consumer_lookup():
    """Even when the tool_calls_to_pass ids ALSO collide, the (id, position) key lets
    the consumer retrieve each position distinctly (bare-id keying collapsed one)."""
    tool_calls_to_pass = [
        {"id": "same", "name": "read_file"},
        {"id": "same", "name": "read_file"},
    ]
    r1 = ActionResult(extracted_content="FIRST"); r1.tool_call_id = "same"
    r2 = ActionResult(extracted_content="SECOND"); r2.tool_call_id = "same"
    paired = _pair_results_to_calls([r1, r2], tool_calls_to_pass)
    # Consumer retrieval by (id, position):
    assert paired.get(("same", 0))[0] == "FIRST"
    assert paired.get(("same", 1))[0] == "SECOND"


def test_tool_call_id_not_in_action_model_dump():
    """tool_call_id must stay OUT of model_dump so it can't be mistaken for an
    action name in act()/multi_act dispatch (regression for the C1 review finding)."""
    a = ActionModel()
    a._tool_call_id = "call_A"
    dump = a.model_dump(exclude_unset=True)
    assert "tool_call_id" not in dump
    assert "_tool_call_id" not in dump
