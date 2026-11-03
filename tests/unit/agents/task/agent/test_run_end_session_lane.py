"""098 CL-02: Agent.run's finally sends the session_completion a run that died
without one never sent — main agent only (a sub-agent shares the session id)."""
import logging
import time
from types import SimpleNamespace

from agents.task.agent.core.run_loop import _end_session_lane


class _Tel:
    def __init__(self):
        self.ends = []

    def capture_session_end(self, **kw):
        self.ends.append(kw)


def _agent(*, sent=False, sub=False):
    return SimpleNamespace(
        _session_end_sent=sent, _is_sub_agent=sub, telemetry_manager=_Tel(),
        state=SimpleNamespace(n_steps=4, stop_reason="cancelled"),
        logger=logging.getLogger("t"))


def test_cancelled_main_run_sends_session_end_once():
    a = _agent()
    _end_session_lane(a, time.time())
    _end_session_lane(a, time.time())
    assert len(a.telemetry_manager.ends) == 1
    end = a.telemetry_manager.ends[0]
    assert end["success"] is False and end["steps"] == 3
    # 098 review: a stop is not a failure — no "session failed" panel text.
    assert end["error_message"] is None
    assert end["metrics"]["stop_reason"] == "cancelled"


def test_an_error_end_still_names_its_reason():
    a = _agent()
    a.state.stop_reason = "error"
    _end_session_lane(a, time.time())
    assert a.telemetry_manager.ends[0]["error_message"] == "error"


def test_normal_end_is_not_sent_twice():
    a = _agent(sent=True)
    _end_session_lane(a, time.time())
    assert a.telemetry_manager.ends == []


def test_sub_agent_never_ends_the_parent_lane():
    a = _agent(sub=True)
    _end_session_lane(a, time.time())
    assert a.telemetry_manager.ends == []


def test_telemetry_error_is_swallowed():
    a = _agent()

    def boom(**kw):
        raise RuntimeError("x")
    a.telemetry_manager.capture_session_end = boom
    _end_session_lane(a, time.time())          # no raise
    assert a._session_end_sent is True
