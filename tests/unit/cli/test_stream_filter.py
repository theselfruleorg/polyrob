"""098 CL-06: the REPL stream box takes only the main agent's deltas, inside a turn."""
import asyncio

from cli.ui.streaming import make_stream_callback


class _R:
    def __init__(self):
        self.got = []

    def on_stream_delta(self, d):
        self.got.append(d)


def _run(cb, agent_id, chunk="x"):
    asyncio.run(cb("s1", agent_id, chunk, 1))


def test_main_id_seeded_from_agent():
    r, main = _R(), {"id": "main-1"}
    cb = make_stream_callback(r, main_agent_id=lambda: main["id"],
                              subagents_active=lambda: 1, turn_active=lambda: True)
    _run(cb, "child-9", "child")
    _run(cb, "main-1", "main")
    assert r.got == ["main"]


def test_unknown_main_drops_while_subagent_active():
    r, active = _R(), {"n": 1}
    cb = make_stream_callback(r, main_agent_id=lambda: "",
                              subagents_active=lambda: active["n"], turn_active=lambda: True)
    _run(cb, "any", "while-child")
    active["n"] = 0
    _run(cb, "any", "alone")
    assert r.got == ["alone"]


def test_delta_outside_turn_ignored():
    r, on = _R(), {"v": False}
    cb = make_stream_callback(r, main_agent_id=lambda: "", turn_active=lambda: on["v"])
    _run(cb, "a", "late")
    on["v"] = True
    _run(cb, "a", "live")
    assert r.got == ["live"]


def test_chat_seeds_main_agent_id_from_the_agent():
    import inspect
    from cli.commands import chat
    src = inspect.getsource(chat)
    assert '_ui_state.main_agent_id = str(getattr(agent, "agent_id"' in src
