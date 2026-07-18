"""Characterization tests for feed_reads.build_session_services (F-2).

Pins the behaviour extracted from webview/server.py::api_services: services.json
cache verbatim, available_actions grouping, service_actions, step-action fallback,
and the empty/corrupt paths.
"""
import json
from pathlib import Path

from agents.task.telemetry.feed_reads import (
    build_session_services,
    build_session_skills,
    build_session_task,
)


class _FakePM:
    def __init__(self, feed_dir: Path):
        self._feed = feed_dir

    def get_feed_dir(self, clean_id: str, user_id=None) -> Path:
        return self._feed


def _session(tmp_path: Path):
    session_dir = tmp_path / "sess"
    feed_dir = session_dir / "feed"
    feed_dir.mkdir(parents=True)
    return session_dir, feed_dir, _FakePM(feed_dir)


def _write(path: Path, obj):
    path.write_text(json.dumps(obj))


def test_services_json_cache_returned_verbatim(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    payload = [{"name": "browser", "type": "controller", "actions": ["click"], "action_count": 1}]
    _write(session_dir / "services.json", payload)
    # A feed file that would produce a different result — must be ignored on cache hit.
    _write(feed_dir / "available_actions_1.json",
           {"type": "available_actions", "data": {"by_service": {"other": ["x"]}}})
    assert build_session_services("sess", path_manager=pm) == payload


def test_corrupt_cache_falls_through_to_available_actions(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    (session_dir / "services.json").write_text("{ bad json ]")
    _write(feed_dir / "available_actions_1.json", {
        "type": "available_actions",
        "data": {"by_service": {"browser": ["click", "type"]}},
    })
    out = build_session_services("sess", path_manager=pm)
    assert out == [{"name": "browser", "type": "controller",
                    "actions": ["click", "type"], "action_count": 2}]


def test_service_actions_when_no_available_actions(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    _write(feed_dir / "service_actions_1.json", {
        "type": "service_actions",
        "data": {"service_name": "email", "service_type": "tool",
                 "available_actions": ["send"], "action_count": 1},
    })
    out = build_session_services("sess", path_manager=pm)
    assert out == [{"name": "email", "type": "tool", "actions": ["send"], "action_count": 1}]


def test_step_action_fallback(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    _write(feed_dir / "step_1.json", {
        "type": "step",
        "data": {"actions": [{"service": "browser", "name": "click"},
                             {"service": "browser", "name": "type"}]},
    })
    out = build_session_services("sess", path_manager=pm)
    assert len(out) == 1
    svc = out[0]
    assert svc["name"] == "browser"
    assert svc["count"] == 2
    assert sorted(svc["actions"]) == ["click", "type"]  # set converted to list


def test_missing_session_returns_empty(tmp_path):
    session_dir = tmp_path / "sess"
    feed_dir = session_dir / "feed"  # not created
    pm = _FakePM(feed_dir)
    assert build_session_services("sess", path_manager=pm) == []


# ---- build_session_task ----

def test_task_json_wins_with_explicit_timestamp(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    _write(session_dir / "task.json", {"task": "do the thing", "timestamp": 123.0})
    # A metadata task that must NOT win (task.json has precedence).
    _write(session_dir / "metadata.json", {"task": "other"})
    assert build_session_task("sess", path_manager=pm) == {"task": "do the thing", "timestamp": 123.0}


def test_task_from_metadata_when_no_task_json(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    _write(session_dir / "metadata.json", {"task": "meta task", "created_at": 99.0})
    assert build_session_task("sess", path_manager=pm) == {"task": "meta task", "timestamp": 99.0}


def test_task_from_session_start_event(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    _write(feed_dir / "session_start_1.json",
           {"type": "session_start", "timestamp": 7.0, "data": {"task": "started"}})
    assert build_session_task("sess", path_manager=pm) == {"task": "started", "timestamp": 7.0}


def test_task_from_first_task_update(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    _write(feed_dir / "task_update_2.json",
           {"type": "task_update", "timestamp": 20.0, "data": {"task": "second"}})
    _write(feed_dir / "task_update_1.json",
           {"type": "task_update", "timestamp": 10.0, "data": {"task": "first"}})
    # Earliest (sorted-first filename) wins.
    assert build_session_task("sess", path_manager=pm) == {"task": "first", "timestamp": 10.0}


def test_task_none_when_nothing_recorded(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    assert build_session_task("sess", path_manager=pm) is None


# ---- build_session_skills ----

def test_skills_json_cache_verbatim(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    payload = [{"id": "skill_a"}, {"id": "skill_b"}]
    _write(session_dir / "skills.json", payload)
    _write(feed_dir / "session_start_1.json",
           {"type": "session_start", "data": {"skills": [{"id": "ignored"}]}})
    assert build_session_skills("sess", path_manager=pm) == payload


def test_skills_from_session_start_when_no_cache(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    _write(feed_dir / "session_start_1.json",
           {"type": "session_start", "data": {"skills": ["s1", "s2"]}})
    assert build_session_skills("sess", path_manager=pm) == ["s1", "s2"]


def test_skills_empty_when_none(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    assert build_session_skills("sess", path_manager=pm) == []
