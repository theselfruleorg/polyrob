"""WS-G follow-up: files the agent writes into the shared data home are born
group-writable, so `polyrob doctor --perms` names only REAL offenders.

Evidence (prod 2026-09-21 00:08Z): the full walk named 19 of the agent's own
per-session files — `agent_state.json` / `tool_calls.json` (0600, the
`tempfile.mkstemp` default) and `control.sqlite3` (0644, SQLite's default,
opened outside `core.sqlite_util`). Each writer now applies the ONE birth rule
`core.data_perms.apply_birth_mode` — the rule `sqlite_util.init_schema` already
carried inline (group-write on, world-write off, never widen beyond that).
"""
import os
import stat
import sys

import pytest

from core.data_perms import apply_birth_mode

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_birth_mode_adds_group_write_and_strips_world_write(tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    os.chmod(p, 0o600)
    apply_birth_mode(p)
    assert _mode(p) == 0o660


def test_birth_mode_keeps_group_read_and_never_widens_to_world(tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    os.chmod(p, 0o646)  # a stray world-write bit
    apply_birth_mode(p)
    assert _mode(p) == 0o664, "world write is stripped, nothing else widens"


def test_birth_mode_is_fail_open_on_a_missing_path(tmp_path):
    apply_birth_mode(tmp_path / "nope")  # must not raise


def test_tool_call_tracker_state_file_is_group_writable(tmp_path):
    from agents.task.agent.tool_call_tracker import ToolCallTracker
    t = ToolCallTracker(session_id="s1")
    target = tmp_path / "data" / "tool_calls.json"
    assert t.save_to_file(target) is True
    assert _mode(target) & stat.S_IWGRP


def test_agent_state_file_is_group_writable(tmp_path):
    from agents.task.agent.agent_state import AgentState
    st = AgentState()
    target = tmp_path / "data" / "agent_state.json"
    assert st.save_to_file(target) is True
    assert _mode(target) & stat.S_IWGRP


def test_session_control_db_is_group_writable(tmp_path):
    from core.session_control import SessionControl
    sc = SessionControl(tmp_path)
    sc.read()  # any access creates the store
    with sc._connect():
        pass
    assert _mode(sc.path) & stat.S_IWGRP
    assert not (_mode(sc.path) & stat.S_IWOTH)
