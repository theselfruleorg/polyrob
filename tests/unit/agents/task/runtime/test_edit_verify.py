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

    ledger = [_step("run_tests"), _step("str_replace")]  # edit is newer than the test
    assert edited_since_last_test(_FakeOrch(ledger)) is True


def test_edit_then_passing_test_is_clean():
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step("str_replace"), _step("run_tests")]  # tested after editing
    assert edited_since_last_test(_FakeOrch(ledger)) is False


def test_no_edit_is_clean():
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step("grep"), _step("session_search")]
    assert edited_since_last_test(_FakeOrch(ledger)) is False


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------

def test_no_ledger_at_all_is_clean():
    from agents.task.runtime.edit_verify import edited_since_last_test

    assert edited_since_last_test(_FakeOrch([])) is False


def test_edit_with_no_test_at_all_needs_verify():
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step("apply_patch")]
    assert edited_since_last_test(_FakeOrch(ledger)) is True


def test_failed_edit_does_not_count_as_edit():
    """A str_replace that errored didn't actually change anything real — the
    ledger's own error signal (same one build_evidence/build_action_evidence
    read) is trusted, not re-derived."""
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step("run_tests"), _step("str_replace", ok=False)]
    assert edited_since_last_test(_FakeOrch(ledger)) is False


def test_failed_run_tests_does_not_clear_the_flag():
    """A run_tests call that errored (framework-level failure OR a non-zero
    exit code — tools/coding/tool.py::run_tests returns self._err() on a
    failing suite) must NOT be read as 'tests are green'."""
    from agents.task.runtime.edit_verify import edited_since_last_test

    ledger = [_step("str_replace"), _step("run_tests", ok=False)]
    assert edited_since_last_test(_FakeOrch(ledger)) is True


def test_multiple_edit_actions_all_recognized():
    from agents.task.runtime.edit_verify import edited_since_last_test

    for action_name in ("str_replace", "apply_patch", "create_file", "move_file", "delete_file"):
        ledger = [_step("run_tests"), _step(action_name)]
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
