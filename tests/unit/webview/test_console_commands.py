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
