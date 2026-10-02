"""W1.2 (console transformation 070): feed writes leave no ``.lock`` files.

Each scheme-A feed write used to wrap the temp + replace in a ``SafeFileLock``
whose ``{seq}_{type}.lock`` file stayed behind (53 of 125 files in one prod
feed). The write now uses a unique dot-prefixed temp file and ``os.replace``;
retention sweeps old ``*.lock`` and ``.*.tmp`` leftovers.
"""
import os
import threading
import time

import pytest


class _FakeEvent:
    """Minimal event: only .name + .properties are used by the feed writer."""
    name = "unknown_test_event"  # -> GenericEventFormatter passthrough

    @property
    def properties(self):
        return {"type": "unknown_test_event", "payload": "x"}


@pytest.fixture
def tele(tmp_path, monkeypatch):
    from agents.task.path import PathManager
    from agents.task.telemetry.sequence import SequenceGenerator
    import agents.task.telemetry.service as svc

    test_pm = PathManager(data_root=str(tmp_path))
    monkeypatch.setattr(svc, "pm", lambda: test_pm)
    monkeypatch.setenv("TELEMETRY_FEED_RETENTION_EVERY", "100000")
    SequenceGenerator.reset_all()
    t = svc.ProductTelemetry()
    t.posthog_enabled = False
    yield t, test_pm
    SequenceGenerator.reset_all()


def _feed_dir(test_pm, session_id):
    return test_pm.get_subdir(test_pm.clean_session_id(session_id), "feed")


def test_one_event_leaves_one_json(tele):
    t, test_pm = tele
    t._save_to_feed_directory(_FakeEvent(), "sess-hygiene")
    feed = _feed_dir(test_pm, "sess-hygiene")
    names = sorted(os.listdir(feed))
    assert len([n for n in names if n.endswith(".json")]) == 1, names
    assert not [n for n in names if n.endswith(".lock")], names
    assert not [n for n in names if n.endswith(".tmp")], names


def test_retention_sweeps_old_locks_and_temps(tele):
    t, test_pm = tele
    feed = _feed_dir(test_pm, "sess-sweep")
    old_lock = feed / "000001_step.lock"
    old_tmp = feed / ".x.tmp"
    fresh_tmp = feed / ".y.tmp"
    for p in (old_lock, old_tmp, fresh_tmp):
        p.write_text("")
    old = time.time() - 600
    os.utime(old_lock, (old, old))
    os.utime(old_tmp, (old, old))

    t._enforce_feed_retention(feed)

    assert not old_lock.exists()
    assert not old_tmp.exists()
    assert fresh_tmp.exists()


def test_two_writers_same_seq_do_not_share_a_temp(tele, monkeypatch):
    t, test_pm = tele
    from agents.task.telemetry.sequence import SequenceGenerator

    gen = SequenceGenerator.get(test_pm.clean_session_id("sess-race"))
    monkeypatch.setattr(gen, "next", lambda: 7)

    barrier = threading.Barrier(2)
    errors = []
    real_dump = __import__("json").dump

    def _slow_dump(obj, fp, **kw):
        # Both writers hold their temp file open at the same time.
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return real_dump(obj, fp, **kw)

    import agents.task.telemetry.service as svc
    monkeypatch.setattr(svc.json, "dump", _slow_dump)
    monkeypatch.setattr(t.logger, "error", lambda *a, **k: errors.append(a))

    threads = [threading.Thread(target=t._save_to_feed_directory,
                                args=(_FakeEvent(), "sess-race")) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    assert not errors, errors
    feed = _feed_dir(test_pm, "sess-race")
    names = sorted(os.listdir(feed))
    assert names == ["000007_unknown_test_event.json"], names


def test_feed_file_is_group_readable(tele):
    """mkstemp births 0600 and os.replace keeps it; on prod the console runs as
    polyrob-web (group polyrob-data) and must read what polyrob-agent wrote."""
    import stat
    t, test_pm = tele
    old = os.umask(0o002)
    try:
        t._save_to_feed_directory(_FakeEvent(), "sess-mode")
    finally:
        os.umask(old)
    feed = _feed_dir(test_pm, "sess-mode")
    (name,) = [n for n in os.listdir(feed) if n.endswith(".json")]
    mode = stat.S_IMODE(os.stat(os.path.join(feed, name)).st_mode)
    assert mode & stat.S_IRGRP, oct(mode)
    assert not mode & stat.S_IWOTH, oct(mode)


def test_rotation_takes_the_append_lock(tele, tmp_path, monkeypatch):
    """The flush appends under events.jsonl.lock; rotation must take the SAME
    lock or a rename can land between an append's open and write."""
    import agents.task.telemetry.service as svc
    t, _ = tele
    taken = []

    class _Rec:
        def __init__(self, path, *a, **k):
            taken.append(os.path.basename(path))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(svc, "SafeFileLock", _Rec)
    monkeypatch.setenv("TELEMETRY_EVENTS_MAX_BYTES", "1")
    f = tmp_path / "events.jsonl"
    f.write_text("{}\n{}\n")
    t._rotate_events_file(f)
    assert taken == ["events.jsonl.lock"], taken
