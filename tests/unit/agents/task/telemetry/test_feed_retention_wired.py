"""Retention wiring (telemetry audit 2026-07-04): `_enforce_feed_retention` existed
but was NEVER called, so per-session feed/ dirs grew unbounded (129 stale dirs on
disk). It must run automatically as feed files accumulate, keeping the dir bounded
to TELEMETRY_FEED_MAX_FILES (+ protected summary files).
"""
import glob

import pytest


class _FakeEvent:
    """Minimal event: only .name + .properties are used by the feed writer."""
    name = "unknown_test_event"  # -> GenericEventFormatter passthrough

    @property
    def properties(self):
        return {"type": "unknown_test_event", "payload": "x"}


def test_feed_retention_runs_and_bounds_dir(tmp_path, monkeypatch):
    from agents.task.path import PathManager
    import agents.task.telemetry.service as svc

    test_pm = PathManager(data_root=str(tmp_path))
    monkeypatch.setattr(svc, "pm", lambda: test_pm)
    # Tiny caps + enforce on every write so the test is deterministic.
    monkeypatch.setenv("TELEMETRY_FEED_MAX_FILES", "5")
    monkeypatch.setenv("TELEMETRY_FEED_RETENTION_EVERY", "1")

    t = svc.ProductTelemetry()
    t.posthog_enabled = False

    session_id = "sess-retention"
    for _ in range(20):
        t._save_to_feed_directory(_FakeEvent(), session_id)

    feed_dir = test_pm.get_subdir(test_pm.clean_session_id(session_id), "feed")
    files = [f for f in glob.glob(str(feed_dir / "*.json"))
             if not f.endswith(("agents.json", "services.json", "task.json"))]
    assert len(files) <= 5, f"feed dir not bounded: {len(files)} files > 5"
