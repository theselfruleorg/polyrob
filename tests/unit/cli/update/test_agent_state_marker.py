"""098 CO-01: the updater reads a turn marker as live only for a live writer."""
import json
import os

from cli.update.agent_state import _read_marker_file


def test_legacy_marker_without_start_time_still_live(tmp_path):
    mp = tmp_path / "turn.active"
    mp.write_text(json.dumps({"pid": os.getpid(), "kind": "owner_chat", "started": 1.0}))
    m, bad = _read_marker_file(mp)
    assert m is not None and bad is None


def test_marker_with_another_start_time_is_stale(tmp_path):
    from core.state.proc import process_identity
    me = process_identity()
    mp = tmp_path / "turn.active"
    mp.write_text(json.dumps({"pid": os.getpid(), "pid_start": "x" + me.start,
                              "boot_id": me.boot, "kind": "owner_chat"}))
    assert _read_marker_file(mp) == (None, None)


def test_marker_of_this_process_is_live(tmp_path):
    from core.state.proc import process_identity
    me = process_identity()
    mp = tmp_path / "turn.active"
    mp.write_text(json.dumps({"pid": me.pid, "pid_start": me.start, "boot_id": me.boot}))
    m, _ = _read_marker_file(mp)
    assert m is not None
