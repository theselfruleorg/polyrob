"""Positional result pairing must skip calls that never executed.

`_pair_results_to_calls` falls back to positional pairing whenever identity pairing
is unavailable (any result missing `tool_call_id`, or duplicate ids). That fallback
walked `tool_calls_to_pass` and `results` with the SAME index — but a call dropped by
pre-execution validation (`Registry.tool_calls_to_actions`) stays in
`tool_calls_to_pass` while contributing NOTHING to `results`.

A drop therefore removes an entry from the middle/front, not the tail, shifting every
later call by one: each got its neighbour's output, and the last real call was
reported to the model as "was NOT executed this step". Silent content
misattribution — the tool_call/response count contract still held, so nothing
surfaced an error.

The consumer's own code proves the mismatch: `_add_tool_messages` skips
validation-failed ids with the comment "This call was never executed (no result to
pair)".
"""
import pytest

from agents.task.agent.core.result_processing import _pair_results_to_calls
from agents.task.agent.views import ActionResult


def _call(cid, name="act"):
    return {"id": cid, "name": name}


def _res(content, tool_call_id=None):
    return ActionResult(extracted_content=content, tool_call_id=tool_call_id)


def test_dropped_middle_call_does_not_shift_later_results():
    """A(dropped), B, C — B must get B's output, C must get C's."""
    calls = [_call("id_A"), _call("id_B"), _call("id_C")]
    # A never ran, so multi_act returned only B's and C's results — and one of them
    # lacks a tool_call_id, which is what forces the positional path.
    results = [_res("B_output", "id_B"), _res("C_output", None)]

    paired = _pair_results_to_calls(
        results, calls, validation_errors={"id_A": "bad params"})

    assert paired[("id_B", 1)][0] == "B_output", "B received another call's output"
    assert paired[("id_C", 2)][0] == "C_output", "C was reported as not executed"


def test_dropped_first_call_does_not_shift():
    calls = [_call("id_A"), _call("id_B")]
    results = [_res("B_output", None)]
    paired = _pair_results_to_calls(
        results, calls, validation_errors={"id_A": "bad"})
    assert paired[("id_B", 1)][0] == "B_output"


def test_dropped_call_gets_no_entry_of_its_own():
    """The dropped call must not be paired to anything — the consumer renders its
    validation error instead."""
    calls = [_call("id_A"), _call("id_B")]
    results = [_res("B_output", None)]
    paired = _pair_results_to_calls(
        results, calls, validation_errors={"id_A": "bad"})
    assert not any(k[0] == "id_A" for k in paired)


def test_no_drops_is_unchanged():
    """Back-compat: with nothing dropped, positional pairing behaves exactly as before."""
    calls = [_call("id_A"), _call("id_B")]
    results = [_res("A_output", None), _res("B_output", None)]
    paired = _pair_results_to_calls(results, calls)
    assert paired[("id_A", 0)][0] == "A_output"
    assert paired[("id_B", 1)][0] == "B_output"


def test_identity_pairing_still_wins_when_all_ids_present():
    """The correct path is untouched: unique ids pair by identity, keyed by id alone."""
    calls = [_call("id_A"), _call("id_B")]
    results = [_res("A_output", "id_A"), _res("B_output", "id_B")]
    paired = _pair_results_to_calls(
        results, calls, validation_errors={"id_A": "ignored here"})
    assert paired["id_A"][0] == "A_output"
    assert paired["id_B"][0] == "B_output"


def test_truncated_execution_still_leaves_later_calls_unpaired():
    """If the executor stopped early (is_done), the calls after the stop legitimately
    have no result and must stay unpaired rather than borrowing one."""
    calls = [_call("id_A"), _call("id_B"), _call("id_C")]
    results = [_res("A_output", None)]  # executor stopped after A
    paired = _pair_results_to_calls(results, calls)
    assert paired[("id_A", 0)][0] == "A_output"
    assert not any(k[0] in ("id_B", "id_C") for k in paired)


def test_drop_plus_truncation_combined():
    calls = [_call("id_A"), _call("id_B"), _call("id_C")]
    results = [_res("B_output", None)]  # A dropped, C never reached
    paired = _pair_results_to_calls(
        results, calls, validation_errors={"id_A": "bad"})
    assert paired[("id_B", 1)][0] == "B_output"
    assert not any(k[0] == "id_C" for k in paired)
