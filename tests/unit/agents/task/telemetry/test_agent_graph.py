"""Characterization tests for the feed→multi-agent-roster read-service.

Pins the behaviour extracted from ``webview/server.py::api_agents`` (F-2): the
agents.json cache short-circuit, feed reconstruction, execution-sequence ordering,
registration/model merge, the metadata.json fallback, and the empty/corrupt paths.
"""
import json
from pathlib import Path

from agents.task.telemetry.agent_graph import build_session_agents


class _FakePM:
    """Minimal PathManager stand-in: feed_dir.parent is the session dir."""

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


def test_cache_hit_short_circuits_and_normalizes(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    # Valid, non-empty agents.json with legacy key aliases.
    _write(session_dir / "agents.json", [
        {"agent_id": "a1", "agent_name": "Alpha", "agent_type": "worker", "model_name": "m1"},
    ])
    # A feed file that WOULD add a different agent — must be ignored on cache hit.
    _write(feed_dir / "agent_registration_zzz_1.json",
           {"type": "agent_registration", "data": {"id": "z9", "name": "Zed"}})

    out = build_session_agents("sess", path_manager=pm)
    assert out == [{"id": "a1", "name": "Alpha", "type": "worker", "model": "m1"}]


def test_corrupt_cache_falls_through_to_feed(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    (session_dir / "agents.json").write_text("{ not valid json ]")
    _write(feed_dir / "agent_registration_a3_1.json",
           {"type": "agent_registration", "data": {"id": "a3", "name": "Gamma", "model_name": "m3"}})

    out = build_session_agents("sess", path_manager=pm)
    assert out == [{"id": "a3", "name": "Gamma", "type": "Unknown", "model": "m3"}]


def test_relationship_execution_sequence_orders_roster(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    _write(feed_dir / "multi_agent_relationship_1.json", {
        "type": "multi_agent_relationship",
        "data": {
            "execution_sequence": ["a2", "a1"],
            "agent_details": [
                {"id": "a1", "name": "Alpha", "type": "worker", "model": "m1"},
                {"id": "a2", "name": "Beta", "type": "lead", "model": "m2"},
            ],
        },
    })
    out = build_session_agents("sess", path_manager=pm)
    assert [a["id"] for a in out] == ["a2", "a1"]
    assert out[0] == {"id": "a2", "name": "Beta", "type": "lead", "model": "m2"}


def test_registration_adds_and_id_sorted_without_sequence(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    _write(feed_dir / "agent_registration_b_1.json",
           {"type": "agent_registration", "data": {"id": "b", "name": "Bee", "model": "mb"}})
    _write(feed_dir / "agent_registration_a_1.json",
           {"type": "agent_registration", "data": {"id": "a", "name": "Ay", "model": "ma"}})
    out = build_session_agents("sess", path_manager=pm)
    # No execution_sequence => sorted by id.
    assert [a["id"] for a in out] == ["a", "b"]


def test_metadata_fallback_merges(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    # No feed agents; roster comes purely from metadata.json.
    _write(session_dir / "metadata.json", {
        "agents": [{"id": "m1", "name": "MetaOne", "type": "worker", "model": "mm"}]
    })
    out = build_session_agents("sess", path_manager=pm)
    assert out == [{"id": "m1", "name": "MetaOne", "type": "worker", "model": "mm"}]


def test_llm_request_backfills_model_on_existing_agent(tmp_path):
    session_dir, feed_dir, pm = _session(tmp_path)
    _write(feed_dir / "agent_registration_a_1.json",
           {"type": "agent_registration", "data": {"id": "a", "name": "Ay"}})  # no model
    _write(feed_dir / "llm_request_1.json",
           {"type": "llm_request", "data": {"agent_id": "a", "model_name": "backfilled"}})
    out = build_session_agents("sess", path_manager=pm)
    assert out == [{"id": "a", "name": "Ay", "type": "Unknown", "model": "backfilled"}]


def test_missing_session_returns_empty(tmp_path):
    # feed_dir does not exist, no agents.json/metadata.json.
    session_dir = tmp_path / "sess"
    feed_dir = session_dir / "feed"  # not created
    pm = _FakePM(feed_dir)
    assert build_session_agents("sess", path_manager=pm) == []
