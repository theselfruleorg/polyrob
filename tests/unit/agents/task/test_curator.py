"""W5 — curator Phase-1 automatic transitions (age + reuse), safety, dry-run."""
import pytest

from agents.task.agent.core.curator import SkillCurator
from modules.skills.skill_usage import SkillUsageStore

DAY = 86400.0


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class _FakeSM:
    def __init__(self):
        self.archived = []

    def delete_skill(self, skill_id, *, user_id, absorbed_into=None):
        self.archived.append((user_id, skill_id))
        return True


@pytest.fixture
def usage(tmp_path):
    return SkillUsageStore(str(tmp_path / "skill_usage.db"), clock=lambda: 1_000_000.0)


def _curator(usage, sm, now, **kw):
    return SkillCurator(sm, usage, clock=lambda: now, **kw)


def test_fresh_unused_skill_is_kept(usage, monkeypatch):
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("s", "u1", "agent")  # created at 1_000_000
    sm = _FakeSM()
    plan = _curator(usage, sm, now=1_000_000.0 + 5 * DAY).apply_automatic_transitions()
    assert "u1/s" in plan["kept"]
    assert sm.archived == []


def test_unused_past_stale_goes_stale(usage, monkeypatch):
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("s", "u1", "agent")
    sm = _FakeSM()
    plan = _curator(usage, sm, now=1_000_000.0 + 45 * DAY).apply_automatic_transitions()
    assert "u1/s" in plan["stale"]
    assert sm.archived == []


def test_unused_past_archive_is_archived(usage, monkeypatch):
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("s", "u1", "agent")
    sm = _FakeSM()
    plan = _curator(usage, sm, now=1_000_000.0 + 120 * DAY).apply_automatic_transitions()
    assert "u1/s" in plan["archived"]
    assert sm.archived == [("u1", "s")]


def test_reused_skill_reactivates(usage, monkeypatch):
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("s", "u1", "agent")
    usage.set_state("curator:u1/s", "stale")  # previously marked stale
    usage.bump_load("s", "u1")                 # but now reused
    sm = _FakeSM()
    plan = _curator(usage, sm, now=1_000_000.0 + 200 * DAY).apply_automatic_transitions()
    assert "u1/s" in plan["reactivated"]
    assert sm.archived == []  # reuse saves it even past archive age


def test_dry_run_applies_nothing(usage, monkeypatch):
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("s", "u1", "agent")
    sm = _FakeSM()
    plan = _curator(usage, sm, now=1_000_000.0 + 200 * DAY, dry_run=True).apply_automatic_transitions()
    assert "u1/s" in plan["archived"]
    assert sm.archived == []  # dry-run: planned but not applied


def test_only_authored_skills_considered(usage, monkeypatch):
    # a 'user' provenance skill must never be touched by the curator
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("user-skill", "u1", "user")
    sm = _FakeSM()
    plan = _curator(usage, sm, now=1_000_000.0 + 500 * DAY).apply_automatic_transitions()
    assert all("user-skill" not in v for v in plan.values())
    assert sm.archived == []


@pytest.mark.asyncio
async def test_run_once_does_phase1_only(usage, monkeypatch):
    """Phase-2 merge was a dead no-op and is removed: run_once returns transitions and
    no longer carries a 'merges' key."""
    c = _curator(usage, _FakeSM(), now=1_000_000.0)
    result = await c.run_once()
    assert "transitions" in result
    assert "merges" not in result


# --- Task 7: episodic retention prune rides the curator tick ---------------

@pytest.mark.asyncio
async def test_run_once_prunes_episodes_on_curator_cadence(usage, monkeypatch):
    monkeypatch.setenv("EPISODIC_RETENTION_DAYS", "90")
    now = 1_000_000.0
    calls = {}

    class _FakeProvider:
        def prune_episodes(self, *, older_than_ts):
            calls["older_than_ts"] = older_than_ts
            return 3

    class _FakeRegistry:
        def active(self):
            return _FakeProvider()

    monkeypatch.setattr(
        "modules.memory.registry.get_memory_registry", lambda: _FakeRegistry())
    c = _curator(usage, _FakeSM(), now=now)
    await c.run_once()
    assert calls["older_than_ts"] == int(now) - 90 * 86400


@pytest.mark.asyncio
async def test_run_once_prune_is_fail_open(usage, monkeypatch):
    """A crashing memory registry must never surface through run_once (the curator's
    Phase-1 result is still returned, no exception propagates)."""
    def boom():
        raise RuntimeError("registry down")
    monkeypatch.setattr("modules.memory.registry.get_memory_registry", boom)
    c = _curator(usage, _FakeSM(), now=1_000_000.0)
    result = await c.run_once()
    assert "transitions" in result
    assert "error" not in result  # Phase-1 itself didn't fail; prune failure is separate/silent


@pytest.mark.asyncio
async def test_run_once_prune_noop_when_provider_lacks_method(usage, monkeypatch):
    """A provider without prune_episodes (e.g. a bespoke external provider) is
    skipped via hasattr, never crashes the tick."""
    class _BareProvider:
        pass

    class _FakeRegistry:
        def active(self):
            return _BareProvider()

    monkeypatch.setattr(
        "modules.memory.registry.get_memory_registry", lambda: _FakeRegistry())
    c = _curator(usage, _FakeSM(), now=1_000_000.0)
    result = await c.run_once()
    assert "transitions" in result


def test_p2_21_already_archived_not_re_archived(usage, monkeypatch):
    """P2-21: a skill archived on one tick is NOT re-archived on the next (provenance
    rows persist after archive, so without the guard the curator re-fired every tick)."""
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("s", "u1", "agent")
    sm = _FakeSM()
    cur = _curator(usage, sm, now=1_000_000.0 + 120 * DAY)

    plan1 = cur.apply_automatic_transitions()
    assert "u1/s" in plan1["archived"]
    assert sm.archived == [("u1", "s")]

    # second tick: the row still exists in provenance, but it's marked archived
    plan2 = cur.apply_automatic_transitions()
    assert "u1/s" not in plan2["archived"], "must not re-archive an already-archived skill"
    assert sm.archived == [("u1", "s")], "delete_skill must not be called again"
