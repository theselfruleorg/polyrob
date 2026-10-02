"""W1.3 — add_to_feed writes the feed file atomically.

The console feed watcher reacts to every new ``*.json`` in the feed folder. A
plain ``open(feed_file, 'w')`` + ``json.dump`` let it see a half-written
``agent_message_*.json``. The write now goes to a dot-prefixed temp file and is
``os.replace``d onto the final name, so the file appears whole or not at all;
a failed write is logged at WARNING with the event type.
"""
import json
import logging

from agents.task.agent.session import SessionManager


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_no_partial_file_is_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from agents.task.path import reset_path_manager
    reset_path_manager()

    sm = SessionManager(base_dir=str(tmp_path))
    cap = _Capture()
    sm.logger.addHandler(cap)

    def _half_then_fail(obj, fp, *a, **k):
        fp.write('{"timestamp": 1, "type": "agent_mess')
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", _half_then_fail)
    try:
        sm.add_to_feed("sess-atomic-1", "agent_message", {"text": "hi"})
    finally:
        sm.logger.removeHandler(cap)

    assert list(tmp_path.rglob("agent_message_*.json")) == []
    assert list(tmp_path.rglob(".*.tmp")) == [], "the temp file must be cleaned up"
    warnings = [r for r in cap.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in cap.records]
    assert "agent_message" in warnings[0].getMessage()


def test_add_to_feed_file_is_group_readable(tmp_path, monkeypatch):
    """mkstemp births 0600; the console (another UID, same group) must read it."""
    import os
    import stat

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from agents.task.path import reset_path_manager
    reset_path_manager()
    sm = SessionManager(base_dir=str(tmp_path))
    old = os.umask(0o002)
    try:
        sm.add_to_feed("sess-mode-1", "agent_message", {"text": "hi"})
    finally:
        os.umask(old)
    (path,) = list(tmp_path.rglob("agent_message_*.json"))
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & stat.S_IRGRP, oct(mode)
    assert not mode & stat.S_IWOTH, oct(mode)


def test_status_event_uses_a_unique_hidden_temp(tmp_path, monkeypatch):
    """The status writer shares the feed folder: its temp file must be
    dot-prefixed (retention sweeps ``.*.tmp``; the watcher skips dot files),
    unique per write, and the final file group-readable."""
    import os
    import stat
    import tempfile as _tf

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from agents.task.path import reset_path_manager
    reset_path_manager()
    sm = SessionManager(base_dir=str(tmp_path))
    seen = []
    real = _tf.mkstemp

    def _spy(*a, **k):
        fd, name = real(*a, **k)
        seen.append(os.path.basename(name))
        return fd, name

    monkeypatch.setattr(_tf, "mkstemp", _spy)
    old = os.umask(0o002)
    try:
        sm._emit_status_event("sess-status-1", "running", "created")
    finally:
        os.umask(old)
    assert seen and all(n.startswith(".") for n in seen), seen
    (path,) = list(tmp_path.rglob("status_*.json"))
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & stat.S_IRGRP, oct(mode)


def test_same_millisecond_events_do_not_overwrite(tmp_path, monkeypatch):
    """Final names are ``{type}_{ms}.json``; two events in one millisecond must
    land as two files (os.replace silently overwrote the first)."""
    import json as _json
    import time as _time

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from agents.task.path import reset_path_manager
    reset_path_manager()
    sm = SessionManager(base_dir=str(tmp_path))
    monkeypatch.setattr(_time, "time", lambda: 1700000000.125)
    sm.add_to_feed("sess-ms-1", "agent_message", {"n": 1})
    sm.add_to_feed("sess-ms-1", "agent_message", {"n": 2})
    sm._emit_status_event("sess-ms-1", "running", "created")
    sm._emit_status_event("sess-ms-1", "completed", "running")

    msgs = sorted(tmp_path.rglob("agent_message_*.json"))
    assert [_json.loads(p.read_text())["data"]["n"] for p in msgs] == [1, 2]
    stats = sorted(tmp_path.rglob("status_*.json"))
    assert [_json.loads(p.read_text())["data"]["status"] for p in stats] == ["running", "completed"]
    assert not list(tmp_path.rglob(".*.tmp"))
