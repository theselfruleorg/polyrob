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
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
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
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "rob", "/status"))
    assert reply is not None and reply.strip()


def test_halt_actually_halts(srv, monkeypatch, tmp_path):
    import os

    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "rob", "/halt"))
    assert reply and ("HALT" in reply.upper() or "PAUSED" in reply.upper())
    assert os.path.exists(os.path.join(str(tmp_path), _PAUSE_FILENAME))
    asyncio.run(srv._maybe_handle_console_command("sess1", "rob", "/resume"))
    assert not os.path.exists(os.path.join(str(tmp_path), _PAUSE_FILENAME))


def test_plain_text_and_unknown_slash_pass_through(srv, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    assert asyncio.run(srv._maybe_handle_console_command("s", "rob", "hello there")) is None
    assert asyncio.run(srv._maybe_handle_console_command("s", "rob", "/etc/passwd?")) is None


def test_session_creating_verbs_pass_through(srv, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    assert asyncio.run(srv._maybe_handle_console_command("s", "rob", "/task do x")) is None


def test_no_in_process_agent_passes_through(srv, monkeypatch):
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: None)
    assert asyncio.run(srv._maybe_handle_console_command("s", "rob", "/status")) is None


# ---------------------------------------------------------------------------
# 043 A10/A45: the console's own /help does not advertise verbs it can't run
# (the chat box short-circuits /task and /new to None — see the test above —
# so /help listing them would just move the dead end into the help text).
# ---------------------------------------------------------------------------

def test_console_help_excludes_task_and_new(srv, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "rob", "/help"))
    assert reply is not None
    assert "/task <goal>" not in reply
    for line in reply.splitlines():
        assert not line.startswith("/new ")


def test_console_help_new_answers_not_available(srv, monkeypatch, tmp_path):
    from webview.console_commands import _CONSOLE_HELP_UNAVAILABLE

    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "rob", "/help new"))
    assert reply == _CONSOLE_HELP_UNAVAILABLE


def test_console_help_task_answers_not_available(srv, monkeypatch, tmp_path):
    from webview.console_commands import _CONSOLE_HELP_UNAVAILABLE

    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(str(tmp_path)))
    reply = asyncio.run(srv._maybe_handle_console_command("sess1", "rob", "/help task"))
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
    assert reply == "Task cancelled."
    assert fake.cancelled == ["sess1"]
