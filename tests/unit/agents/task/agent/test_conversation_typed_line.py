"""104: a typed REPL line reaches the feed, so the console can draw it."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from agents.task.agent.conversation import _record_typed_line


def test_the_typed_line_reaches_the_feed():
    manager = MagicMock()
    agent = SimpleNamespace(orchestrator=SimpleNamespace(
        session_manager=manager, session_id="s1", user_id="owner"))
    _record_typed_line(agent, "hello")
    args = manager.add_to_feed.call_args.args
    assert args[:2] == ("s1", "user_message")
    assert args[2]["text"] == "hello" and args[2]["sender_user_id"] == "owner"


def test_a_feed_fault_never_breaks_the_turn():
    _record_typed_line(SimpleNamespace(), "hello")
