"""2026-10-13 review C2: the rollover bridge must read the REAL step ledger,
``agent.history`` — ``AgentState`` has no ``history`` field."""
from agents.task.agent.views import ActionResult, AgentHistory, AgentHistoryList
from agents.task.loop_types.browser_views import BrowserStateHistory
from agents.task.session.cleanup import (
    _closing_turn_called_done, _final_assistant_text, _resolve_chat_outcome)


class _State:  # AgentState shape: no `history`
    n_steps = 3


class _Agent:
    _is_sub_agent = False

    def __init__(self, history):
        self.state = _State()
        self.history = history


def _ledger(text, done):
    step = AgentHistory(
        model_output=None,
        result=[ActionResult(extracted_content=text, is_done=done)],
        state=BrowserStateHistory(url="", title="", tabs=[], interacted_element=[]))
    return AgentHistoryList(history=[step])


def test_the_closing_reply_is_read_from_agent_history():
    agent = _Agent(_ledger("Sent the invoice to Bob.", True))
    assert _final_assistant_text(agent) == "Sent the invoice to Bob."
    assert _closing_turn_called_done(agent) is True


def test_an_evicted_session_that_ended_in_done_is_upgraded():
    class _Orch:
        agents = {"executor": _Agent(_ledger("done", True))}
    assert _resolve_chat_outcome("suspended", _Orch()) == "done"
