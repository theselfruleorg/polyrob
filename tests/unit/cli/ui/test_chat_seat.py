"""113 W2: the terminal's chats (cli/ui/chat_seat.py, cli/ui/commands/h_chats.py)."""
import io
import types

import pytest

from cli.ui import chat_seat
from cli.ui.commands import h_chats
from core.surfaces import chats
from core.surfaces.session_chat_registry import SessionChatRegistry


class _Container:
    def __init__(self):
        self._svc = {}

    def get_service(self, name):
        return self._svc.get(name)

    def register_service(self, name, value):
        self._svc[name] = value


@pytest.fixture
def box(tmp_path):
    container = _Container()
    container.register_service("session_chat_registry",
                               SessionChatRegistry(str(tmp_path / "surfaces.db")))
    return container


def _orch():
    return types.SimpleNamespace()


def _ctx(container, orch, args, user="u_a"):
    buf = io.StringIO()
    ctx = types.SimpleNamespace(container=container, orchestrator=orch, user_id=user,
                                args=list(args), session_id="s")
    ctx.emit = lambda text, title=None: buf.write(text + "\n")
    return ctx, buf


def test_a_run_starts_in_main_and_its_episode_carries_the_chat_key(box):
    orch = _orch()
    out = chat_seat.attach(box, orch, user_id="u_a", session_id="s1")
    base = chat_seat.base_key("u_a")
    assert out == {"key": base, "name": "main", "note": ""}
    assert orch._chat_thread_key == base
    reg = box.get_service("session_chat_registry")
    assert reg.resolve(base)["session_id"] == "s1"
    assert reg.resolve_by_session_id("s1")["session_key"] == base   # the bridge's lookup


def test_the_next_start_continues_the_same_chat(box):
    chat_seat.attach(box, _orch(), user_id="u_a", session_id="s1")
    out = chat_seat.attach(box, _orch(), user_id="u_a", session_id="s2")
    reg = box.get_service("session_chat_registry")
    assert out["key"] == chat_seat.base_key("u_a")
    assert reg.resolve(out["key"])["session_id"] == "s2"            # one row, a new pointer
    assert len(reg.chat_rows(user_id="u_a")) == 1


def test_new_and_chat_pick_the_run_chat(box):
    chat_seat.attach(box, _orch(), user_id="u_a", session_id="s1")
    out = chat_seat.attach(box, _orch(), user_id="u_a", session_id="s2", new_label="")
    assert out["key"].endswith(":chat:c2") and out["name"] == "c2"
    back = chat_seat.attach(box, _orch(), user_id="u_a", session_id="s3", chat_ref="main")
    assert back["name"] == "main" and back["note"] == ""
    miss = chat_seat.attach(box, _orch(), user_id="u_a", session_id="s4", chat_ref="nope")
    assert "No chat named" in miss["note"] and miss["name"] == "main"   # it still starts


def test_a_chat_store_fault_never_stops_the_terminal():
    orch = _orch()
    assert chat_seat.attach(None, orch, user_id="u_a", session_id="s1")["key"] is None
    assert not hasattr(orch, "_chat_thread_key")


def test_verbs_say_where_this_run_is_and_where_the_next_start_goes(box):
    orch = _orch()
    chat_seat.attach(box, orch, user_id="u_a", session_id="s1")
    ctx, buf = _ctx(box, orch, ["notes"])
    h_chats.h_new(ctx)
    assert "New chat: notes" in buf.getvalue() and "start me again" in buf.getvalue()
    assert orch._chat_thread_key == chat_seat.base_key("u_a")       # the run did not move
    ctx, buf = _ctx(box, orch, [])
    h_chats.h_chats(ctx)
    listing = buf.getvalue()
    assert "main" in listing and "this run" in listing and "your next start" in listing
    ctx, buf = _ctx(box, orch, ["home"])
    h_chats.h_rename(ctx)       # "the chat you are in" is THIS RUN's chat, not the focus
    reg = box.get_service("session_chat_registry")
    assert reg.chat_row(chat_seat.base_key("u_a"))["label"] == "home"
    assert reg.chat_row(chat_seat.base_key("u_a") + ":chat:c2")["label"] == "notes"
    ctx, buf = _ctx(box, orch, [])
    h_chats.h_close(ctx)        # this run is in main: main is never closed
    assert "stays open" in buf.getvalue()
    ctx, buf = _ctx(box, orch, ["main"])
    h_chats.h_chat(ctx)
    assert "Now in chat: main" in buf.getvalue()                    # back where the run is


def test_two_terminal_tenants_do_not_share_chats(box):
    chat_seat.attach(box, _orch(), user_id="u_a", session_id="s1", new_label="")
    ctx, buf = _ctx(box, _orch(), [], user="u_b")
    h_chats.h_chats(ctx)
    assert "c2" not in buf.getvalue()


def test_a_focus_move_at_a_terminal_restarts_in_that_chat(box, monkeypatch):
    from cli.ui.commands.registry import ReplExit
    monkeypatch.setattr(chat_seat, "can_restart", lambda: True)
    monkeypatch.setitem(chat_seat._RESTART, "wanted", False)
    orch = _orch()
    chat_seat.attach(box, orch, user_id="u_a", session_id="s1")
    ctx, buf = _ctx(box, orch, ["notes"])
    with pytest.raises(ReplExit):
        h_chats.h_new(ctx)
    text = buf.getvalue()
    assert "New chat: notes." in text and chat_seat.RESTARTING in text
    assert "start me again" not in text
    assert chat_seat._RESTART["wanted"] is True
    monkeypatch.setitem(chat_seat._RESTART, "wanted", False)
    ctx, buf = _ctx(box, orch, [])
    h_chats.h_chats(ctx)                    # a read never restarts
    assert chat_seat._RESTART["wanted"] is False
    ctx, buf = _ctx(box, orch, ["home"])
    h_chats.h_rename(ctx)                   # nor does a rename
    assert chat_seat._RESTART["wanted"] is False


def test_the_restart_command_line_drops_the_chat_choice():
    argv = ["python", "-m", "cli", "--new", "--chat", "notes", "--plain", "--chat=c2", "chat"]
    assert chat_seat.restart_argv(argv) == ["python", "-m", "cli", "--plain", "chat"]
