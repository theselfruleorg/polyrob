"""session_turn_end_notice (review 2026-10-29 H1, L1).

H1: a turn that ended by the R1 conversational exit already answered with
send_message — it gets no "My turn ended early" notice.
L1: "Last tool error" quotes an ACTION result only, never a step-level
LLM/provider error (raw provider text stays out of chat).
"""
from types import SimpleNamespace

from agents.task.runtime.run_outcome import session_turn_end_notice


class _Action:
    def __init__(self, name):
        self._d = {name: {}}

    def model_dump(self, **_):
        return dict(self._d)


def _item(error, *, with_action=True):
    out = SimpleNamespace(action=[_Action("cronjob_edit")]) if with_action else None
    return SimpleNamespace(model_output=out, result=[SimpleNamespace(error=error)])


def _task_agent(reason, items):
    state = SimpleNamespace(stop_reason=reason, max_steps=50, exhausted_summary=None,
                            n_steps=len(items))
    agent = SimpleNamespace(state=state, history=SimpleNamespace(history=list(items)),
                            _is_sub_agent=False)
    orch = SimpleNamespace(agents={"main": agent})
    return SimpleNamespace(get_orchestrator=lambda sid: orch)


def test_conversational_exit_is_quiet():
    ta = _task_agent("conversational_exit", [])
    assert session_turn_end_notice(ta, "s1", spoke=True) == ""
    assert session_turn_end_notice(ta, "s1", spoke=False) == ""


def test_loop_stop_still_speaks():
    ta = _task_agent("loop", [_item("cronjob_edit: Nothing to change")])
    text = session_turn_end_notice(ta, "s1", spoke=True)
    assert text.startswith("⚠️ My turn ended early")
    assert "Last tool error: cronjob_edit: Nothing to change" in text


def test_a_provider_step_error_is_not_quoted():
    provider = "Error code: 402 - {'error': {'message': 'Insufficient credits sk-...'}}"
    ta = _task_agent("failures", [_item("cronjob_edit: bad id"),
                                  _item(provider, with_action=False)])
    text = session_turn_end_notice(ta, "s1")
    assert "Insufficient credits" not in text
    assert "Last tool error: cronjob_edit: bad id" in text


def test_only_a_provider_error_gives_no_error_line():
    ta = _task_agent("failures", [_item("LLM call failed: 500", with_action=False)])
    text = session_turn_end_notice(ta, "s1")
    assert text and "Last tool error" not in text
