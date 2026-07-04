"""W2-D — skill provenance + usage store."""
import pytest

from modules.skills.skill_usage import SkillUsageStore


@pytest.fixture
def store(tmp_path):
    return SkillUsageStore(str(tmp_path / "skill_usage.db"))


def test_provenance_roundtrip(store):
    store.record_provenance("s1", "u1", "agent")
    p = store.get_provenance("s1", "u1")
    assert p["created_by"] == "agent"


def test_bump_load_increments(store):
    store.bump_load("s1", "u1")
    store.bump_load("s1", "u1")
    assert store.get_usage("s1", "u1")["load_count"] == 2


def test_bump_anon_is_noop(store):
    store.bump_load("s1", "")
    assert store.get_usage("s1", "")["load_count"] == 0


def test_list_authored_joins_usage(store):
    store.record_provenance("s1", "u1", "agent")
    store.record_provenance("s2", "u1", "background_review")
    store.bump_load("s1", "u1")
    rows = store.list_authored(user_id="u1", created_by=["agent", "background_review"])
    by_id = {r["skill_id"]: r for r in rows}
    assert by_id["s1"]["load_count"] == 1
    assert by_id["s2"]["load_count"] == 0  # authored, never used (curator candidate)


def test_curator_state(store):
    assert store.get_state("last_run") is None
    store.set_state("last_run", "123.0")
    assert store.get_state("last_run") == "123.0"
