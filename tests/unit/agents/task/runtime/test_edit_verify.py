"""I-3 / H3 — verify-before-done: deterministic "edited since last successful
run_tests" check (dedup decision D1).

Pure, deterministic, ledger-derived (reuses ``evidence.py::_walk_ledger`` — the
SAME ledger walker ``build_evidence``/``build_action_evidence`` already read),
no LLM, no timestamp markers. Mirrors the fake-ledger style already proven
against ``_walk_ledger`` in ``tests/unit/agents/task/runtime/test_evidence.py``
(``_Action.model_dump(exclude_unset=True)`` — the real signature
``run_outcome._action_name`` calls).
"""
from types import SimpleNamespace

from core.action_names import namespaced_action_name


def _n(method):
    """The key the ledger really carries for a ``coding`` method (review B1:
    the old bare literals passed here while production never matched)."""
    return namespaced_action_name("coding", method)


class _Action:
    def __init__(self, name, params=None):
        self._d = {name: params or {}}

    def model_dump(self, exclude_unset=True):
        return dict(self._d)


class _Result:
    def __init__(self, error=None, content=None):
        self.error = error
        self.extracted_content = content


class _Step:
    def __init__(self, actions, results):
        self.model_output = SimpleNamespace(action=list(actions))
        self.result = list(results)


class _Agent:
    def __init__(self, steps, is_sub=False):
        self.history = SimpleNamespace(history=list(steps))
        self._is_sub_agent = is_sub


class _FakeOrch:
    """Wraps a flat list of steps into a single-agent orchestrator, matching
    the shape ``_walk_ledger`` reads: ``orchestrator.agents[*].history.history``.
    """

    def __init__(self, ledger_steps):
        self.agents = {"main": _Agent(list(ledger_steps))}


def _step(action_name, ok=True, content=None):
    return _Step(
        actions=[_Action(action_name)],
        results=[_Result(error=None if ok else "boom", content=content)],
    )


# ---------------------------------------------------------------------------
# Core acceptance-criteria cases (brief Step 4.1)
# ---------------------------------------------------------------------------

def test_edit_after_test_needs_verify():
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step(_n("run_tests")), _step(_n("str_replace"))]  # edit is newer than the test
    assert edited_since_last_test(_FakeOrch(ledger)) is True


def test_edit_then_passing_test_is_clean():
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step(_n("str_replace")), _step(_n("run_tests"))]  # tested after editing
    assert edited_since_last_test(_FakeOrch(ledger)) is False


def test_no_edit_is_clean():
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step(_n("grep")), _step("session_search")]
    assert edited_since_last_test(_FakeOrch(ledger)) is False


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------

def test_no_ledger_at_all_is_clean():
    from agents.task.runtime.edit_verify import edited_since_last_test

    assert edited_since_last_test(_FakeOrch([])) is False


def test_edit_with_no_test_at_all_needs_verify():
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step(_n("apply_patch"))]
    assert edited_since_last_test(_FakeOrch(ledger)) is True


def test_failed_edit_does_not_count_as_edit():
    """A str_replace that errored didn't actually change anything real — the
    ledger's own error signal (same one build_evidence/build_action_evidence
    read) is trusted, not re-derived."""
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step(_n("run_tests")), _step(_n("str_replace"), ok=False)]
    assert edited_since_last_test(_FakeOrch(ledger)) is False


def test_failed_run_tests_does_not_clear_the_flag():
    """A run_tests call that errored (framework-level failure OR a non-zero
    exit code — tools/coding/tool.py::run_tests returns self._err() on a
    failing suite) must NOT be read as 'tests are green'."""
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step(_n("str_replace")), _step(_n("run_tests"), ok=False)]
    assert edited_since_last_test(_FakeOrch(ledger)) is True


def test_multiple_edit_actions_all_recognized():
    from agents.task.runtime.edit_verify import edited_since_last_test

    for method in ("str_replace", "apply_patch", "create_file", "move_file", "delete_file"):
        action_name = _n(method)
        ledger = [_step(_n("run_tests")), _step(action_name)]
        assert edited_since_last_test(_FakeOrch(ledger)) is True, action_name


def test_none_orchestrator_fails_open():
    from agents.task.runtime.edit_verify import edited_since_last_test

    assert edited_since_last_test(None) is False


def test_broken_orchestrator_fails_open():
    """Any introspection error must never block a finish (D1: fail-open)."""
    from agents.task.runtime.edit_verify import edited_since_last_test

    class _Boom:
        @property
        def agents(self):
            raise RuntimeError("boom")

    assert edited_since_last_test(_Boom()) is False


def test_missing_test_result_never_verifies_an_edit():
    from agents.task.runtime.edit_verify import edited_since_last_test
    assert edited_since_last_test(_FakeOrch([
        _step(_n("apply_patch")), _Step([_Action(_n("run_tests"))], [])]))


def test_agent_iteration_order_is_not_global_time():
    from agents.task.runtime.edit_verify import edited_since_last_test
    orch = _FakeOrch([_step(_n("apply_patch"))])
    orch.agents["child"] = _Agent([_step(_n("run_tests"))], is_sub=True)
    assert edited_since_last_test(orch)
    orch.agents = dict(reversed(list(orch.agents.items())))
    assert edited_since_last_test(orch)


def test_cross_agent_verification_requires_test_start_after_edit_finish():
    from agents.task.runtime.edit_verify import edited_since_last_test
    edit, test = _step(_n("apply_patch")), _step(_n("run_tests"))
    common = {"clock_id": "same-process", "workspace": "/fixture", "session_id": "s1", "ok": True}
    edit.result[0].metadata = {"execution_receipt": {**common, "started_ns": 10, "finished_ns": 20}}
    receipt = {**common, "started_ns": 15, "finished_ns": 30}
    test.result[0].metadata = {"execution_receipt": receipt}
    orch = _FakeOrch([edit])
    orch.agents["child"] = _Agent([test], is_sub=True)
    assert edited_since_last_test(orch)  # overlapped the write
    receipt["started_ns"] = 21
    assert not edited_since_last_test(orch)
    receipt["workspace"] = "/another-workspace"
    assert edited_since_last_test(orch)


def test_late_evidence_is_retained_with_an_omission_marker():
    from agents.task.runtime.evidence import build_evidence, MAX_LEDGER_LINES
    orch = _FakeOrch([_step(_n("grep"))] * 100 + [_step(_n("run_tests"), content="FINAL_TEST_EVIDENCE")])
    pack = build_evidence(orch)
    assert len(pack.ledger) == MAX_LEDGER_LINES
    assert "FINAL_TEST_EVIDENCE" in pack.ledger[-1]
    assert any("omitted" in line for line in pack.ledger)


# ---------------------------------------------------------------------------
# B1 regression (coding-agent review 2026-09-24): the names come from a REAL
# Controller registration of the real CodingTool actions, not from literals.
# ---------------------------------------------------------------------------

def _registered_coding_names():
    import logging
    import threading

    import agents.task.agent.service  # noqa: F401 — controller<->orchestrator cycle
    from tools.coding.tool import CodingTool
    from tools.controller.registry.service import Registry
    from tools.controller.service import Controller

    class _Tool:
        def get_actions(self):
            return {name: getattr(CodingTool, name) for name in dir(CodingTool)
                    if not name.startswith("_")
                    and getattr(getattr(CodingTool, name), "_param_model", None) is not None}

    c = object.__new__(Controller)
    c.logger = logging.getLogger("b1")
    c._lock = threading.RLock()
    c._tools = {}
    c._action_list_cache = None
    c._tool_list_cache = None
    c.registry = Registry()
    c.session_id = c.user_id = c.workspace_dir = None
    c.add_tool("coding", _Tool())
    return set(c.registry.get_action_names())


def test_verify_sets_match_real_registered_names():
    from agents.task.runtime.edit_verify import _EDIT_ACTIONS, TEST_ACTIONS
    from agents.task.runtime.evidence import OUTPUT_ACTION_ALLOWLIST

    names = _registered_coding_names()
    assert "coding_str_replace" in names and "str_replace" not in names
    assert TEST_ACTIONS <= names
    coding_edits = {n for n in _EDIT_ACTIONS if n.startswith("coding_")}
    assert len(coding_edits) == 5 and coding_edits <= names
    assert {"coding_str_replace", "coding_apply_patch"} <= OUTPUT_ACTION_ALLOWLIST


def test_real_names_trip_the_gate():
    from agents.task.runtime.edit_verify import edited_since_last_test

    names = _registered_coding_names()
    edit = next(n for n in names if n.endswith("str_replace"))
    test = next(n for n in names if n.endswith("run_tests"))
    assert edited_since_last_test(_FakeOrch([_step(test), _step(edit)])) is True
    assert edited_since_last_test(_FakeOrch([_step(edit), _step(test)])) is False


# ---------------------------------------------------------------------------
# 068 G7: never ask for a test run the deploy cannot perform, or for prose
# ---------------------------------------------------------------------------

def _edit(path):
    return _Step(actions=[_Action(_n("str_replace"), {"path": path})],
                 results=[_Result()])


def _refused_tests(text):
    return _Step(actions=[_Action(_n("run_tests"))], results=[_Result(error=text)])


def test_nudge_skipped_after_a_posture_refusal():
    from agents.task.runtime.edit_verify import verify_nudge_applies
    orch = _FakeOrch([_edit("tools/x.py"), _refused_tests(
        "code execution unavailable on this deploy: the agent identity cannot open "
        "the Docker socket")])
    assert verify_nudge_applies(orch) is False


def test_nudge_skipped_when_every_edit_is_prose():
    from agents.task.runtime.edit_verify import verify_nudge_applies
    assert verify_nudge_applies(_FakeOrch([_edit("docs/rules.md"), _edit("notes.txt")])) is False


def test_nudge_kept_for_code_and_for_an_ordinary_test_failure():
    from agents.task.runtime.edit_verify import verify_nudge_applies
    assert verify_nudge_applies(_FakeOrch([_edit("docs/a.md"), _edit("tools/x.py")])) is True
    assert verify_nudge_applies(_FakeOrch([_edit("tools/x.py"),
                                           _refused_tests("2 failed")])) is True
    assert verify_nudge_applies(_FakeOrch([_step(_n("str_replace"))])) is True


def test_a_prose_move_is_prose_and_a_move_to_code_is_code():
    """Codex B13: coding_move_file carries src_path/dest_path, not path."""
    from agents.task.runtime.edit_verify import verify_nudge_applies

    def _move(src, dest):
        return _Step(actions=[_Action(_n("move_file"), {"src_path": src, "dest_path": dest})],
                     results=[_Result()])
    assert verify_nudge_applies(_FakeOrch([_move("README.md", "docs/README.md")])) is False
    assert verify_nudge_applies(_FakeOrch([_move("notes.md", "tools/notes.py")])) is True


def test_the_real_str_replace_field_is_read():
    from agents.task.runtime.edit_verify import verify_nudge_applies
    step = _Step(actions=[_Action(_n("str_replace"), {"file_path": "docs/a.md"})],
                 results=[_Result()])
    assert verify_nudge_applies(_FakeOrch([step])) is False
