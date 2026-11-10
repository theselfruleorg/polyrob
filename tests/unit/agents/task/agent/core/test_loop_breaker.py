"""The loop breaker escalates (prod 2026-10-26..28: identical calls ran 69-150 times).

The old detector injected guidance and then RESET its counter, so it warned every
few steps forever and never stopped a run. Now: N identical calls with the same
result -> guidance; one more identical call -> not executed (error naming the
repetition and the earlier result); one more -> the run ends with
``StopReason.LOOP``. A call whose result CHANGES is not a loop (polling).
"""
import asyncio
import logging
import types
from collections import deque

from agents.task.agent.core.loop_detection import LoopDetectionMixin
from agents.task.agent.core.step_execution import StepExecutionMixin
from agents.task.agent.views import ActionResult


class _Action:
    def __init__(self, name, **params):
        self._d = {name: params}

    def model_dump(self, **_):
        return self._d


class _Output:
    def __init__(self, *actions, memory=""):
        self.action = list(actions)
        self.current_state = types.SimpleNamespace(memory=memory)


class _MM:
    def __init__(self):
        self.added = []
        self.ephemeral = []

    def add_message(self, msg):
        self.added.append(msg)

    def push_ephemeral_message(self, msg):
        self.ephemeral.append(msg)

    def inject_runtime_guidance(self, *a, **k):
        pass


class _Agent(LoopDetectionMixin):
    def __init__(self, max_reps=2):
        self.logger = logging.getLogger("loop-breaker-test")
        self._previous_actions = deque(maxlen=25)
        self._action_repetition_counter = 0
        self._max_allowed_repetitions = max_reps
        self._last_result = []
        self.message_manager = _MM()
        self.controller = types.SimpleNamespace(get_action_names=lambda: ["done"])
        self.state = types.SimpleNamespace(
            stopped=False, paused=False, loop_warning_count=0, stop_reason=None,
            reset_loop_detection=lambda: None)


def _run_step(agent, output, result_text="same result", error=None):
    """One step as the executor does it: guard, (execute), record."""
    blocked = agent._loop_guard_check(output)
    if blocked is not None:
        agent._last_result = blocked
        agent._record_action_for_loop_detection(output)
        return blocked, False
    agent._last_result = [ActionResult(extracted_content=result_text, error=error)]
    agent._record_action_for_loop_detection(output)
    return agent._last_result, True


def _interventions(agent):
    return [m for m in agent.message_manager.added if "LOOP DETECTED" in m.content]


def test_identical_calls_warn_then_block_then_stop():
    a = _Agent(max_reps=2)
    call = lambda: _Output(_Action("filesystem_read_file", path="a.jsonl", char_offset=0))
    executed = []
    for _ in range(3):  # counter reaches 2 on the third identical step -> guidance
        _, ran = _run_step(a, call())
        executed.append(ran)
    assert executed == [True, True, True]
    assert len(_interventions(a)) == 1
    assert a._action_repetition_counter == 2  # not reset by the intervention

    res, ran = _run_step(a, call())
    assert ran is False
    assert len(res) == 1 and res[0].error
    assert "filesystem_read_file" in res[0].error
    assert "same result" in res[0].error  # the earlier result is named
    assert a.state.stop_reason is None
    assert a._take_loop_stop() is None

    res, ran = _run_step(a, call())
    assert ran is False and res[0].error
    assert a.state.stop_reason == "loop"
    assert a._take_loop_stop()
    assert a._take_loop_stop() is None  # taken once


def test_blocked_step_returns_one_error_per_action():
    a = _Agent(max_reps=1)
    out = lambda: _Output(_Action("message", action="send"), _Action("message", action="send"))
    _run_step(a, out())
    _run_step(a, out())  # counter 1 -> guidance
    res, ran = _run_step(a, out())
    assert ran is False and len(res) == 2 and all(r.error for r in res)


def test_changing_result_is_not_a_loop():
    a = _Agent(max_reps=2)
    for i in range(10):
        _, ran = _run_step(a, _Output(_Action("wallet_balance", chain="base")),
                           result_text=f"balance {i}")
        assert ran is True
    assert _interventions(a) == []
    assert a.state.stop_reason is None


def test_a_different_call_after_the_warning_clears_the_escalation():
    a = _Agent(max_reps=2)
    same = lambda: _Output(_Action("cronjob_edit", job_id="j1", deliver="telegram"))
    for _ in range(3):
        _run_step(a, same())
    assert len(_interventions(a)) == 1
    _, ran = _run_step(a, _Output(_Action("cronjob_list")))
    assert ran is True
    _, ran = _run_step(a, same())  # a fresh start: the world may have changed
    assert ran is True
    assert a.state.stop_reason is None


def test_reset_for_a_new_run_clears_the_escalation():
    a = _Agent(max_reps=2)
    for _ in range(3):
        _run_step(a, _Output(_Action("x")))
    a._reset_loop_guard()
    _, ran = _run_step(a, _Output(_Action("x")))
    assert ran is True


def test_alternating_pattern_does_not_fire_every_step():
    a = _Agent(max_reps=99)
    for name in ("a", "b") * 4:
        _run_step(a, _Output(_Action(name)), result_text=name)
    # A-B-A-B fires once per 4 steps at most, not on every step after the 4th.
    assert 1 <= len(_interventions(a)) <= 2


# --- the executor: a blocked call never reaches the controller ----------------

class _Exec(StepExecutionMixin, _Agent):
    def __init__(self):
        _Agent.__init__(self, max_reps=1)
        self._deferred_mcp_actions = []
        self.tool_call_tracker = None
        self.calls = 0
        self.actions_need_browser = lambda actions: False
        agent = self

        class _Ctrl:
            registry = types.SimpleNamespace(list_action_names=lambda: ["noop"])

            def has_action(self, name):
                return True

            def get_action_names(self):
                return ["noop", "done"]

            async def multi_act(self, actions, execution_context):
                agent.calls += 1
                return [ActionResult(extracted_content="ok") for _ in actions]

        self.controller = _Ctrl()
        self.state.n_steps = 1
        self.state.total_actions_count = 0

    def detect_action_loop(self, *a, **k):
        return False, None

    def _build_execution_context(self, browser_context):
        return types.SimpleNamespace(metadata={})


def test_execute_actions_does_not_run_a_blocked_call():
    a = _Exec()

    async def step():
        out = _Output(_Action("noop", x=1))
        res = await a._execute_actions(out, None, None, None, None)
        a._record_action_for_loop_detection(out)
        return res

    asyncio.run(step())
    asyncio.run(step())  # guidance
    assert a.calls == 2
    res = asyncio.run(step())
    assert a.calls == 2  # not executed
    assert res[0].error and a._last_result is res


# --- review 2026-10-29 H2: observe/poll/navigation work is never blocked ------

def _never_blocked(a, out, n, result_text):
    for _ in range(n):
        _, ran = _run_step(a, out(), result_text=result_text)
        assert ran is True
    assert a.state.stop_reason is None
    assert a._take_loop_stop() is None


def test_a_feed_read_by_scrolling_is_never_blocked():
    a = _Agent(max_reps=2)
    _never_blocked(a, lambda: _Output(_Action("browser_scroll_down")), 8, "⬇️ Scrolled down")
    notes = [m for m in a.message_manager.added if "This call is not blocked" in m.content]
    assert len(notes) == 1 and _interventions(a) == []   # one note, no escalation
    assert all(not r.error for r in a._last_result)  # no error result is added


def test_a_load_more_click_is_never_blocked():
    a = _Agent(max_reps=2)
    _never_blocked(a, lambda: _Output(_Action("browser_click_element", index=41)), 6,
                   "🖱️ Clicked element with index 41: Load more")


def test_a_quiet_build_wait_is_never_blocked():
    a = _Agent(max_reps=2)
    _never_blocked(a, lambda: _Output(_Action("process_wait", job_id="j1", timeout=60)), 6,
                   "j1: running (still running after 60s)\n...compiling")


def test_an_observe_only_cycle_is_never_blocked():
    a = _Agent(max_reps=99)
    for name in ("browser_scroll_down", "browser_extract_page_content") * 4:
        _, ran = _run_step(a, _Output(_Action(name)), result_text=name)
        assert ran is True
    assert a.state.stop_reason is None


def test_a_non_observe_action_in_the_step_keeps_the_block():
    a = _Agent(max_reps=2)
    out = lambda: _Output(_Action("browser_scroll_down"),
                          _Action("filesystem_read_file", path="a.md"))
    for _ in range(3):
        _run_step(a, out())
    _, ran = _run_step(a, out())
    assert ran is False


def test_identical_failing_calls_are_left_to_the_retry_wall():
    """One counter per failure: the 093 retry wall (execution.py) blocks an
    identical failing call and ends the run (FAILURES); the loop breaker does
    not count it too, so one call never heads for two different stops."""
    a = _Agent(max_reps=2)
    out = lambda: _Output(_Action("cronjob_edit", job_id="j1", deliver="telegram"))
    for _ in range(6):
        _, ran = _run_step(a, out(), result_text="", error="Nothing to change: ...")
        assert ran is True
    assert _interventions(a) == []
    assert a._action_repetition_counter == 0
    assert a.state.stop_reason is None


def test_an_identical_two_call_cycle_escalates():
    """L4: A-B-A-B with the same arguments and results -> guidance, the next call
    of the cycle is not run, the one after ends the run."""
    a = _Agent(max_reps=99)
    A = lambda: _Output(_Action("filesystem_read_file", path="a.md"))
    B = lambda: _Output(_Action("cronjob_list"))
    for out, text in ((A, "a"), (B, "b"), (A, "a"), (B, "b")):
        _, ran = _run_step(a, out(), result_text=text)
        assert ran is True
    assert len(_interventions(a)) == 1
    res, ran = _run_step(a, A())
    assert ran is False and res[0].error
    assert a.state.stop_reason is None
    _, ran = _run_step(a, B())
    assert ran is False
    assert a.state.stop_reason == "loop"


def test_a_cycle_broken_by_a_new_call_clears_the_escalation():
    a = _Agent(max_reps=99)
    A = lambda: _Output(_Action("filesystem_read_file", path="a.md"))
    B = lambda: _Output(_Action("cronjob_list"))
    for out, text in ((A, "a"), (B, "b"), (A, "a"), (B, "b")):
        _run_step(a, out(), result_text=text)
    _, ran = _run_step(a, _Output(_Action("cronjob_show", job_id="j1")))
    assert ran is True
    _, ran = _run_step(a, A(), result_text="a")
    assert ran is True


# --- a run of reworded sends to one recipient (prod 2026-11-06) --------------

def _send(n, target="owner", **extra):
    return _Output(_Action("message", surface="telegram", target=target,
                           text=f"reworded line {n}", action="send", **extra))


def _send_run(agent, count, target="owner"):
    return [_run_step(agent, _send(i, target), result_text=f"sent id {i}") for i in range(count)]


def test_reworded_sends_warn_then_refuse_then_end_the_run():
    agent = _Agent()
    outcomes = _send_run(agent, 4)
    assert all(ran for _, ran in outcomes)
    assert any("3 messages to this recipient" in m.content for m in agent.message_manager.added)

    results, ran = _run_step(agent, _send(4), result_text="sent")
    assert ran is False and "MESSAGE NOT SENT" in results[0].error
    assert agent._take_loop_stop() is None

    results, ran = _run_step(agent, _send(5), result_text="sent")
    assert ran is False and "RUN ENDED" in results[0].error
    assert agent._take_loop_stop()
    assert agent.state.stop_reason is not None


def test_a_send_refused_as_a_duplicate_still_counts():
    agent = _Agent()
    for i in range(4):
        _run_step(agent, _send(i), error="message NOT sent: This exact text already reached")
    results, ran = _run_step(agent, _send(9), result_text="sent")
    assert ran is False and "MESSAGE NOT SENT" in results[0].error


def test_send_message_and_message_to_owner_are_one_run():
    agent = _Agent()
    for i in range(2):
        _run_step(agent, _Output(_Action("send_message", text=f"a{i}")), result_text=f"s{i}")
        _run_step(agent, _send(i), result_text=f"t{i}")
    _, ran = _run_step(agent, _send(7), result_text="sent")
    assert ran is False


def test_a_work_step_restarts_the_count():
    agent = _Agent()
    _send_run(agent, 4)
    _run_step(agent, _Output(_Action("filesystem_append_file", file_path="a", content="b")),
              result_text="ok")
    outcomes = _send_run(agent, 4)
    assert all(ran for _, ran in outcomes)


def test_another_recipient_is_not_the_same_run():
    agent = _Agent()
    _send_run(agent, 4, target="@channel")
    outcomes = _send_run(agent, 2, target="-100room")
    assert all(ran for _, ran in outcomes)


def test_a_message_read_is_not_a_send():
    agent = _Agent()
    for i in range(8):
        output = _Output(_Action("message", action="posts", last=i))
        _, ran = _run_step(agent, output, result_text=f"posts {i}")
        assert ran is True


def test_a_new_run_clears_the_count():
    agent = _Agent()
    _send_run(agent, 4)
    agent._reset_loop_guard()
    _, ran = _run_step(agent, _send(0), result_text="sent")
    assert ran is True
