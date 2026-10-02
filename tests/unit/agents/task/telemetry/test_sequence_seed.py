"""W1.4 (console transformation 070): the telemetry sequence survives a restart.

A fresh process used to start every session's sequence at 0, so a resumed
session wrote ``000001_<type>.json`` again and overwrote older events. The
generator now seeds from the highest leading integer on disk.
"""
import pytest

from agents.task.telemetry.sequence import SequenceGenerator


@pytest.fixture(autouse=True)
def _clean():
    SequenceGenerator.reset_all()
    yield
    SequenceGenerator.reset_all()


def test_seed_from_disk(tmp_path):
    (tmp_path / "000041_step_0003.json").write_text("{}")
    (tmp_path / "000007_tool_result.json").write_text("{}")
    (tmp_path / "agents.json").write_text("{}")  # no leading integer
    (tmp_path / "000099_step.lock").write_text("")  # not a .json
    gen = SequenceGenerator.get("sess-seed", feed_dir=tmp_path)
    assert gen.next() == 42


def test_unreadable_dir_starts_at_one(tmp_path):
    gen = SequenceGenerator.get("sess-missing", feed_dir=tmp_path / "does-not-exist")
    assert gen.next() == 1


def test_cached_generator_is_not_reseeded(tmp_path):
    (tmp_path / "000005_step.json").write_text("{}")
    gen = SequenceGenerator.get("sess-cache", feed_dir=tmp_path)
    assert gen.next() == 6
    assert gen.next() == 7
    # Another file on disk with a lower seq must not move the counter back,
    # and a higher one must not jump it either: the cached generator is kept.
    (tmp_path / "000100_step.json").write_text("{}")
    again = SequenceGenerator.get("sess-cache", feed_dir=tmp_path)
    assert again is gen
    assert again.next() == 8


def test_feed_writer_continues_after_disk_seq(tmp_path, monkeypatch):
    """The service passes the feed folder, so a restarted writer continues."""
    from agents.task.path import PathManager
    import agents.task.telemetry.service as svc

    test_pm = PathManager(data_root=str(tmp_path))
    monkeypatch.setattr(svc, "pm", lambda: test_pm)
    sid = test_pm.clean_session_id("sess-resume")
    feed = test_pm.get_subdir(sid, "feed")
    (feed / "000041_step_0003.json").write_text("{}")

    class _Ev:
        name = "unknown_test_event"
        properties = {"type": "unknown_test_event"}

    t = svc.ProductTelemetry()
    t.posthog_enabled = False
    t._save_to_feed_directory(_Ev(), "sess-resume")
    assert (feed / "000042_unknown_test_event.json").exists()
