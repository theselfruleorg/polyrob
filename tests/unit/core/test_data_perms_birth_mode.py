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
    assert _mode(p) == 0o660, "all access by other users is removed"


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


@pytest.mark.parametrize('link_kind', ['symlink', 'hardlink'])
def test_birth_mode_never_changes_a_link_target(tmp_path, link_kind):
    target = tmp_path / 'private'
    target.write_text('secret')
    target.chmod(0o600)
    link = tmp_path / 'alias'
    if link_kind == 'symlink':
        link.symlink_to(target)
    else:
        os.link(target, link)
    apply_birth_mode(link)
    assert _mode(target) == 0o600


def test_birth_mode_refuses_a_symlinked_parent(tmp_path):
    target = tmp_path / 'private'
    target.mkdir()
    file = target / 'data'
    file.write_text('secret')
    file.chmod(0o600)
    alias = tmp_path / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    apply_birth_mode(alias / 'data')
    assert _mode(file) == 0o600
