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

    def inject_runtime_guidance(self, text, *, origin=None, source=None):
        self.injected.append({"text": text, "origin": origin,
                              "metadata": {"source": source}})


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


def test_empty_action_texts_have_no_contradiction_and_no_absent_tools():
    """F12: the empty-action texts no longer deny the planning allowance, no longer
    point at "the functions below", and name only registered actions."""
    a = _Agent()
    for _ in range(4):
        a._validate_and_intervene(_EMPTY)
    texts = "\n".join(m["text"] for m in a.message_manager.injected)
    assert "no 'thinking mode' or 'planning phase'" not in texts
    assert "functions below" not in texts
    assert "mcp_execute_tool" not in texts
    assert "filesystem_write_file" not in texts
    assert "done(" in texts


# --- a REJECTED call is a failed step, not a planning turn (prod 2026-10-26/28) ---

class _RejectingController(_Controller):
    def get_last_validation_errors(self):
        return {"c1": "Validation failed for 'defi_data_wallet_holdings': "
                      "Missing required: ['chain']."}


@pytest.mark.parametrize("issue", ["bad_args", "unknown_tool"])
def test_a_rejected_call_is_a_failed_step_not_a_planning_turn(issue):
    """Every call failed validation (a required arg missing) or named no action.
    That was granted the tool-free planning allowance ("Planning turn noted"),
    so the failure counter rose only every second rejected step."""
    from agents.task.agent.core import call_quality
    a = _Agent()
    a.controller = _RejectingController()
    for n in (1, 2, 3):
        call_quality.set_call_issue(a, issue)
        assert a._validate_and_intervene(_EMPTY) is False
        assert a.state.consecutive_failures == n
        res = a._last_result
        assert len(res) == 1 and res[0].error
        assert "Planning" not in (res[0].extracted_content or "")
        assert "did NOT run" in res[0].error
    assert issue != "bad_args" or "chain" in a._last_result[0].error
    assert a._empty_action_counter == 0     # a call was made: not a tool-free step


def test_a_rejected_call_does_not_spend_the_planning_allowance(monkeypatch):
    from agents.task.agent.core import call_quality
    monkeypatch.setenv("ALLOWED_REASONING_TURNS", "1")
    a = _Agent()
    a.controller = _RejectingController()
    call_quality.set_call_issue(a, "bad_args")
    a._validate_and_intervene(_EMPTY)
    a._validate_and_intervene(_EMPTY)       # a real tool-free step after it
    assert a._last_result[0].metadata.get("planning_turn") is True
