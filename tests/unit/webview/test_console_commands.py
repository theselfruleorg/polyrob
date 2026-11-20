"""030 WS-B6 (Q1, G3): a slash verb typed into the console chat box routes
through the shared chat-command plane — /halt halts; it is never forwarded to
the LLM as prose. Unknown slashes still reach the agent (a prose question may
start with a path)."""
from core.autonomy_control import PAUSE_FILENAME as _PAUSE_FILENAME  # 031: the one record
import asyncio

import pytest


@pytest.fixture()
def srv(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    import webview.server as server
    return server


class _Cfg:
    def __init__(self, d):
        self.data_dir = d


class _Container:
    def __init__(self, d):
        self.config = _Cfg(d)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, d):
        self.container = _Container(d)


def test_known_verb_is_handled_inline(srv, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "alice", "/status"))
    assert reply is not None and reply.strip()


def test_halt_actually_halts(srv, monkeypatch, tmp_path):
    import os

    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "alice", "/halt"))
    assert reply and ("HALT" in reply.upper() or "PAUSED" in reply.upper())
    assert os.path.exists(os.path.join(str(tmp_path), _PAUSE_FILENAME))
    asyncio.run(srv._maybe_handle_console_command("sess1", "alice", "/resume"))
    assert not os.path.exists(os.path.join(str(tmp_path), _PAUSE_FILENAME))


def test_plain_text_and_unknown_slash_pass_through(srv, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    assert asyncio.run(srv._maybe_handle_console_command("s", "alice", "hello there")) is None
    assert asyncio.run(srv._maybe_handle_console_command("s", "alice", "/etc/passwd?")) is None


def test_session_creating_verbs_pass_through(srv, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    assert asyncio.run(srv._maybe_handle_console_command("s", "alice", "/task do x")) is None


def test_no_in_process_agent_passes_through(srv, monkeypatch):
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: None)
    assert asyncio.run(srv._maybe_handle_console_command("s", "alice", "/status")) is None


# ---------------------------------------------------------------------------
# 043 A10/A45: the console's own /help does not advertise a verb it can't run
# (the chat box short-circuits /task to None — see the test above). 113: /new
# is a chat verb the chat box runs, so /help lists it.
# ---------------------------------------------------------------------------

def test_console_help_excludes_task(srv, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "alice", "/help"))
    assert reply is not None
    assert "/task <goal>" not in reply
    assert "/new" in reply and "/chats" in reply


def test_console_help_new_is_the_chat_verb(srv, monkeypatch, tmp_path):
    from webview.console_commands import _CONSOLE_HELP_UNAVAILABLE

    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "alice", "/help new"))
    assert reply and reply != _CONSOLE_HELP_UNAVAILABLE and "chat" in reply


def test_console_help_task_answers_not_available(srv, monkeypatch, tmp_path):
    from webview.console_commands import _CONSOLE_HELP_UNAVAILABLE

    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "alice", "/help task"))
    assert reply == _CONSOLE_HELP_UNAVAILABLE


def test_console_cancel_permitted_for_a_non_owner_on_their_own_session(srv, monkeypatch, tmp_path):
    """A10: the shared /cancel gate must not regress the console's existing
    contract — the mutating route already checked `current_user_id ==
    session_owner_id` before ``_maybe_handle_console_command`` is ever
    reached (webview/server.py), so a non-owner here is always acting on
    their OWN session and must still be able to cancel it."""
    class _AgentWithCancel(_Agent):
        def __init__(self, d):
            super().__init__(d)
            self.cancelled = []

        async def cancel_session_by_id(self, session_id, force=False):
            self.cancelled.append(session_id)
            return True

    fake = _AgentWithCancel(str(tmp_path))
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: fake)
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "u_stranger", "/cancel"))
    assert reply.startswith("Stopped.") and "/pause" in reply
    assert fake.cancelled == ["sess1"]


# ---------------------------------------------------------------------------
# Audit 2026-10-03 CLI11/CLI12.
# ---------------------------------------------------------------------------

def test_unknown_command_shaped_verb_gets_the_help_answer(srv, monkeypatch, tmp_path):
    """CLI11: a typo verb is answered like Telegram answers it, never an agent turn."""
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    reply = asyncio.run(srv._maybe_handle_console_command("s", "alice", "/statsu"))
    assert reply and "Unknown command /statsu" in reply and "/help" in reply
    from webview.console_commands import looks_like_console_verb
    assert looks_like_console_verb("/statsu") is True
    assert looks_like_console_verb("/etc/passwd?") is False


def test_a_routing_failure_on_a_known_verb_is_an_error_reply(srv, monkeypatch, tmp_path):
    """CLI12: the verb line never goes to the agent as prose when routing raises."""
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    import surfaces.telegram.harness as harness

    async def _boom(*a, **k):
        raise RuntimeError("routing broke")

    monkeypatch.setattr(harness, "_handle_command", _boom)
    reply = asyncio.run(srv._maybe_handle_console_command(
        "s", "alice", "/send 1 native to 0xabc on base go"))
    assert reply is not None and reply.startswith("/send could not run here")


def test_a_reply_with_buttons_keeps_them_for_the_console(srv, monkeypatch, tmp_path):
    """The chat box draws the SAME buttons Telegram does (reply_buttons): a nav
    reply's next-step actions reach the JSON answer, not only its text."""
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    import surfaces.telegram.harness as harness
    from core.surfaces.command_reply import CommandReply
    from core.surfaces.envelopes import Action
    from webview.console_commands import reply_buttons_json

    async def _menu(*a, **k):
        return CommandReply("Pick one.", actions=(Action("Status", "/status", "primary"),))

    monkeypatch.setattr(harness, "_handle_command", _menu)
    reply = asyncio.run(srv._maybe_handle_console_command("s", "alice", "/model"))
    assert reply == "Pick one."
    assert reply_buttons_json(reply) == [
        {"label": "Status", "command": "/status", "style": "primary"}]
    assert reply_buttons_json("plain text") == []
