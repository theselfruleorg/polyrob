"""A5: the output judge grades what the USER received (the last send_message),
not the done() record the user never sees. A sub-agent's done() text is its
report to the parent, so a sub-agent keeps the final-step reading."""
from types import SimpleNamespace

from agents.task.agent.core.output_validation import OutputValidationMixin
from agents.task.agent.views import ActionResult


class _Action:
    def __init__(self, name, **params):
        self._d = {name: params}

    def model_dump(self, exclude_unset=True):
        return self._d


def _agent(steps, last_result, sub=False):
    a = OutputValidationMixin.__new__(OutputValidationMixin)
    a.history = SimpleNamespace(history=steps)
    a._last_result = last_result
    a._is_sub_agent = sub
    return a


def _step(actions, results):
    return SimpleNamespace(model_output=SimpleNamespace(action=actions), result=results)


def test_the_judge_reads_the_last_send_message_not_the_record():
    done = ActionResult(is_done=True, extracted_content="Answered the owner's question")
    steps = [_step([_Action("send_message", text="The answer is 42."), _Action("done", text="x")],
                   [ActionResult(extracted_content="Message sent to user (non-blocking)"), done])]
    out = _agent(steps, [done])._judged_output()
    assert out == "The answer is 42."
    assert "Answered the owner" not in out


def test_nothing_sent_is_named_for_the_judge():
    done = ActionResult(is_done=True, extracted_content="record")
    out = _agent([], [done])._judged_output()
    assert out == "(the agent sent the user no message)"


def test_a_sub_agent_is_judged_on_its_report():
    done = ActionResult(is_done=True, extracted_content="report for the parent")
    assert _agent([], [done], sub=True)._judged_output() == "report for the parent"
