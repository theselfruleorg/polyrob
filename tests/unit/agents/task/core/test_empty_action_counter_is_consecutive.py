"""The tool-free-response counter must mean what every message says it means.

`_empty_action_counter` was only ever reset inside the escalation branch — never on a
productive step, and never by `reset_for_continuation()` (which resets
`state.consecutive_failures` but not this Agent-level attribute). So it counted empty
responses over the whole life of the agent, while the logs and the injected
intervention text both said "N consecutive steps without function calls".

Consequences it caused:
  * a model that emitted one tool-free step at step 3 and another at step 44 drew the
    "2 consecutive steps" escalation, with an intervention scolding it for something
    it had not done;
  * ALLOWED_REASONING_TURNS degenerated from "one planning turn per run" into "one per
    two empty responses ever";
  * nothing incremented `state.consecutive_failures` for this failure mode — the only
    `increment_failures()` call sat in a branch `_validate_model_output` had already
    made unreachable — so `_too_many_failures()` could not end a run whose model
    simply refuses to call tools.
"""
import pytest

from agents.task.agent.core.step_execution import StepExecutionMixin


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class _State:
    def __init__(self):
        self.n_steps = 0
        self.consecutive_failures = 0

    def increment_failures(self):
        self.consecutive_failures += 1


class _MM:
    def __init__(self):
        self.injected = []

    def inject_user_guidance(self, msgs, **k):
        self.injected.extend(msgs)


class _Controller:
    def get_action_names(self):
        return ["a", "b"]


class _Agent(StepExecutionMixin):
    def __init__(self):
        self.logger = _Logger()
        self.state = _State()
        self.message_manager = _MM()
        self.controller = _Controller()
        self.tool_call_tracker = None
        self.task = "do the thing"
        self._last_result = None

    def _validate_model_output(self, model_output):
        return bool(model_output)


_EMPTY = None          # falsy -> _validate_model_output returns False
_PRODUCTIVE = object()  # truthy -> valid


def test_a_productive_step_resets_the_counter(monkeypatch):
    monkeypatch.setenv("ALLOWED_REASONING_TURNS", "1")
    a = _Agent()

    a._validate_and_intervene(_EMPTY)          # 1st empty -> planning turn
    assert a._empty_action_counter == 1

    a._validate_and_intervene(_PRODUCTIVE)     # real work
    assert a._empty_action_counter == 0, (
        "a productive step did not reset the counter — empties 40 steps apart still "
        "escalate as 'consecutive'"
    )


def test_far_apart_empties_do_not_escalate():
    """One empty, lots of work, one empty => two planning turns, no intervention."""
    a = _Agent()

    a._validate_and_intervene(_EMPTY)
    for _ in range(40):
        a._validate_and_intervene(_PRODUCTIVE)
    a._validate_and_intervene(_EMPTY)

    sources = [m["metadata"]["source"] for m in a.message_manager.injected]
    assert "thinking_loop_detector" not in sources, (
        "escalated on two empties 40 steps apart"
    )


def test_genuinely_consecutive_empties_still_escalate():
    """The backstop must keep working — this is the whole point of the counter."""
    a = _Agent()
    for _ in range(4):
        a._validate_and_intervene(_EMPTY)

    sources = [m["metadata"]["source"] for m in a.message_manager.injected]
    assert "thinking_loop_detector" in sources


def test_escalation_counts_against_the_run_failure_budget():
    """Without this, a model that never calls tools burns the entire max_steps budget:
    _too_many_failures() had no input for this failure mode."""
    a = _Agent()
    for _ in range(4):
        a._validate_and_intervene(_EMPTY)
    assert a.state.consecutive_failures > 0, (
        "the thinking-loop escalation never reached state.consecutive_failures, so "
        "max_failures can't end an unproductive run"
    )


def test_validate_and_intervene_returns_true_only_for_valid_output():
    a = _Agent()
    assert a._validate_and_intervene(_PRODUCTIVE) is True
    assert a._validate_and_intervene(_EMPTY) is False


def test_reset_for_continuation_clears_the_counter():
    """A new conversational turn starts fresh; the counter lives on the Agent, so
    state.consecutive_failures = 0 never reached it."""
    import inspect

    from agents.task.agent.core import safety_lifecycle

    src = inspect.getsource(safety_lifecycle.SafetyLifecycleMixin.reset_for_continuation)
    assert "_empty_action_counter = 0" in src, (
        "reset_for_continuation does not clear _empty_action_counter — an empty "
        "response in a previous turn still counts toward this turn's escalation"
    )
