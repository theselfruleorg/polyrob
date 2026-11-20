"""/goals and /goal buttons are built below the seat (cron/owner_goals.py), so every
seat that draws CommandReply actions (the chat apps, the console) gets them."""
from types import SimpleNamespace

from core.surfaces.command_reply import CommandReply


class _Board:
    def __init__(self, goals):
        self._goals = goals

    def list_recent(self, **_kw):
        return list(self._goals)


def test_the_list_opens_each_goal(monkeypatch):
    from cron.owner_goals import goal_buttons
    board = _Board([SimpleNamespace(id="abcdef1234", title="Ship the report", status="ready")])
    buttons = goal_buttons("u1", "", [], board=board)
    assert [(b.label, b.line) for b in buttons] == [("Ship the report", "/goal show abcdef12")]


def test_one_goal_offers_its_next_steps(monkeypatch):
    import cron.owner_goals as og
    goal = SimpleNamespace(id="abcdef1234", title="t", status="paused")
    monkeypatch.setattr(og, "_resolve_goal", lambda board, uid, ref: (goal, None))
    lines = [b.line for b in og.goal_buttons("u1", "", ["show", "abcdef12"], board=object())]
    assert lines == ["/goal retry abcdef12", "/goal stop abcdef12", "/goals"]


def test_goal_command_reply_carries_the_actions(monkeypatch):
    import cron.owner_goals as og
    monkeypatch.setattr(og, "goal_reply", lambda *a, **k: "Goal abcdef12: paused")
    goal = SimpleNamespace(id="abcdef1234", title="t", status="paused")
    monkeypatch.setattr(og, "_resolve_goal", lambda board, uid, ref: (goal, None))
    monkeypatch.setattr("core.surfaces.nav.actions",
                        lambda uid, buttons: [b.line for b in buttons])
    reply = og.goal_command_reply("u1", "", ["show", "abcdef12"], board=object())
    assert isinstance(reply, CommandReply) and reply.text == "Goal abcdef12: paused"
    assert "/goal retry abcdef12" in reply.actions


def test_the_seat_holds_no_goal_button_procedure():
    import importlib.util
    assert importlib.util.find_spec("surfaces.telegram.goal_buttons") is None
