"""W5 — curator Phase-1 automatic transitions (idle time since last use), safety,
dry-run; 095 W12 — incubation, pins, owner skills, the contribution report."""
import json
import sqlite3
from types import SimpleNamespace

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

    def delete_skill(self, skill_id, *, user_id, absorbed_into=None, **_kw):
        self.archived.append((user_id, skill_id))
        return True


@pytest.fixture
def usage(tmp_path):
    return SkillUsageStore(str(tmp_path / "skill_usage.db"), clock=lambda: 1_000_000.0)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Every curator reads its pin stores from an empty temp home, never the real one."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))


def _curator(usage, sm, now, **kw):
    kw.setdefault("data_dir", str(usage.db_path).rsplit("/", 1)[0])
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
    now = 1_000_000.0 + 200 * DAY
    usage._now = lambda: now - 2 * DAY         # but used again two days ago
    usage.bump_load("s", "u1")
    sm = _FakeSM()
    plan = _curator(usage, sm, now=now).apply_automatic_transitions()
    assert "u1/s" in plan["reactivated"]
    assert sm.archived == []  # recent use saves it even past archive age


def test_last_used_at_drives_staleness_not_load_count(usage, monkeypatch):
    """X-4: one load long ago is not use today — the idle clock is last_used_at."""
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("old", "u1", "agent")
    usage.record_provenance("fresh", "u1", "agent")
    for _ in range(20):
        usage.bump_load("old", "u1")            # 20 loads, all at day 0
    now = 1_000_000.0 + 120 * DAY
    usage._now = lambda: now - 3 * DAY
    usage.bump_load("fresh", "u1")              # one load three days ago
    sm = _FakeSM()
    plan = _curator(usage, sm, now=now).apply_automatic_transitions()
    assert "u1/old" in plan["archived"]
    assert "u1/fresh" in plan["kept"]
    assert sm.archived == [("u1", "old")]


def test_used_then_idle_goes_stale(usage, monkeypatch):
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("s", "u1", "agent")
    now = 1_000_000.0 + 100 * DAY
    usage._now = lambda: now - 45 * DAY
    usage.bump_load("s", "u1")
    plan = _curator(usage, _FakeSM(), now=now).apply_automatic_transitions()
    assert "u1/s" in plan["stale"]


def test_incubating_skill_never_archived(usage, monkeypatch):
    """A skill younger than the incubation window is never staled or archived,
    whatever the stale/archive ages say (no use yet is no evidence)."""
    from agents.task.agent.core import curator as cur_mod
    monkeypatch.setenv("CURATOR_STALE_DAYS", "1")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "2")
    usage.record_provenance("s", "u1", "agent")
    sm = _FakeSM()
    now = 1_000_000.0 + (cur_mod.INCUBATION_DAYS - 1) * DAY
    plan = _curator(usage, sm, now=now).apply_automatic_transitions()
    assert "u1/s" in plan["kept"]
    assert plan["stale"] == [] and plan["archived"] == []
    assert sm.archived == []
    # one day past the window the same skill archives
    plan = _curator(usage, sm, now=now + 2 * DAY).apply_automatic_transitions()
    assert "u1/s" in plan["archived"]


def _write_pins(home, *, cron=(), goals=()):
    home.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(home / "cron.db")
    c.execute("CREATE TABLE cron_jobs (id TEXT, user_id TEXT, payload TEXT, "
              "enabled INTEGER, status TEXT)")
    for i, (uid, skills, enabled, status) in enumerate(cron):
        c.execute("INSERT INTO cron_jobs VALUES (?,?,?,?,?)",
                  (f"j{i}", uid, json.dumps({"skills": skills}), enabled, status))
    c.commit(); c.close()
    g = sqlite3.connect(home / "goals.db")
    g.execute("CREATE TABLE goals (id TEXT, user_id TEXT, payload TEXT, status TEXT)")
    for i, (uid, skills, status) in enumerate(goals):
        g.execute("INSERT INTO goals VALUES (?,?,?,?)",
                  (f"g{i}", uid, json.dumps({"skills": skills}), status))
    g.commit(); g.close()


def test_skill_pinned_by_live_job_or_goal_counts_as_used(usage, tmp_path, monkeypatch):
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    for sid in ("by-job", "by-goal", "by-dead-job", "by-done-goal"):
        usage.record_provenance(sid, "u1", "agent")
    home = tmp_path / "pins"
    _write_pins(home,
                cron=[("u1", ["by-job"], 1, "scheduled"),
                      ("u1", ["by-dead-job"], 0, "cancelled")],
                goals=[("u1", ["by-goal"], "ready"), ("u1", ["by-done-goal"], "done")])
    sm = _FakeSM()
    plan = _curator(usage, sm, now=1_000_000.0 + 200 * DAY,
                    data_dir=str(home)).apply_automatic_transitions()
    assert {"u1/by-job", "u1/by-goal"} <= set(plan["kept"])
    assert set(plan["archived"]) == {"u1/by-dead-job", "u1/by-done-goal"}


def test_unreadable_pin_store_archives_nothing(usage, tmp_path, monkeypatch):
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("s", "u1", "agent")
    home = tmp_path / "pins"
    home.mkdir()
    (home / "cron.db").write_bytes(b"not a database")
    sm = _FakeSM()
    plan = _curator(usage, sm, now=1_000_000.0 + 200 * DAY,
                    data_dir=str(home)).apply_automatic_transitions()
    assert plan["archived"] == [] and sm.archived == []


def test_dry_run_applies_nothing(usage, monkeypatch):
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("s", "u1", "agent")
    sm = _FakeSM()
    plan = _curator(usage, sm, now=1_000_000.0 + 200 * DAY, dry_run=True).apply_automatic_transitions()
    assert "u1/s" in plan["archived"]
    assert sm.archived == []  # dry-run: planned but not applied


def test_owner_skill_never_auto_archived(usage, monkeypatch):
    """A 'user' provenance skill is never archived or staled: an idle one files ONE
    owner proposal line (a self_modification 'propose' event) instead."""
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("user-skill", "u1", "user")
    events = []
    monkeypatch.setattr(SkillCurator, "_self_mod_ev", staticmethod(
        lambda action, skill_id, user_id, **kw: events.append((action, skill_id, kw))))
    sm = _FakeSM()
    cur = _curator(usage, sm, now=1_000_000.0 + 500 * DAY)
    plan = cur.apply_automatic_transitions()
    assert sm.archived == []
    assert "u1/user-skill" not in plan["archived"] + plan["stale"]
    assert len(plan["proposed"]) == 1 and plan["proposed"][0].startswith("user-skill:")
    assert [(a, s, kw["reason"]) for a, s, kw in events] == [("propose", "user-skill", "idle")]
    # the next tick files nothing more
    plan2 = cur.apply_automatic_transitions()
    assert plan2["proposed"] == [] and len(events) == 1 and sm.archived == []


def test_owner_skill_between_stale_and_archive_is_only_kept(usage, monkeypatch):
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    usage.record_provenance("user-skill", "u1", "user")
    plan = _curator(usage, _FakeSM(), now=1_000_000.0 + 60 * DAY).apply_automatic_transitions()
    assert "u1/user-skill" in plan["kept"] and plan["proposed"] == []


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


# --- 095 W12 step 3: the contribution report (proposal only) ------------------

def _run(lane, skills, *, useful=False, verified=None, source="receipt"):
    meta = {"lane": lane, "skills_loaded": list(skills), "useful": useful,
            "signal_source": source}
    if verified:
        meta["verified"] = verified
    return SimpleNamespace(meta=meta)


def _worse_runs(n_with=6, n_without=6, source="receipt"):
    # loaded: 1 good of n_with; without: all good (same lane)
    runs = [_run("lane-a", ["s"], useful=(i == 0), source=source) for i in range(n_with)]
    runs += [_run("lane-a", [], useful=True, source=source) for _ in range(n_without)]
    return runs


def test_contribution_findings_reports_clearly_worse_skill():
    from agents.task.agent.core.curator import contribution_findings, contribution_line
    out = contribution_findings(["s"], _worse_runs())
    assert len(out) == 1
    assert contribution_line(out[0]) == "s: 1/6 useful when loaded vs 6/6 without — patch or archive?"


def test_contribution_report_needs_min_sample():
    from agents.task.agent.core.curator import contribution_findings
    assert contribution_findings(["s"], _worse_runs(n_with=4)) == []
    assert contribution_findings(["s"], _worse_runs(n_without=4)) == []


def test_contribution_report_ignores_self_signals():
    from agents.task.agent.core.curator import contribution_findings
    assert contribution_findings(["s"], _worse_runs(source="self")) == []
    assert contribution_findings(["s"], _worse_runs(source="none")) == []
    # judge-verified runs count as good
    runs = [_run("lane-a", ["s"], source="judge", verified="unverified") for _ in range(5)]
    runs += [_run("lane-a", [], source="judge", verified="verified") for _ in range(5)]
    assert len(contribution_findings(["s"], runs)) == 1


def test_contribution_compares_same_lane_only():
    from agents.task.agent.core.curator import contribution_findings
    runs = [_run("lane-a", ["s"], useful=False) for _ in range(6)]
    runs += [_run("lane-b", [], useful=True) for _ in range(6)]  # other lane: not compared
    assert contribution_findings(["s"], runs) == []


def test_contribution_similar_rate_is_not_reported():
    from agents.task.agent.core.curator import contribution_findings
    runs = [_run("lane-a", ["s"], useful=(i < 3)) for i in range(6)]
    runs += [_run("lane-a", [], useful=(i < 4)) for i in range(6)]
    assert contribution_findings(["s"], runs) == []


def test_contribution_report_files_one_line_and_never_archives(usage, monkeypatch):
    usage.record_provenance("s", "u1", "agent")
    events = []
    monkeypatch.setattr(SkillCurator, "_self_mod_ev", staticmethod(
        lambda action, skill_id, user_id, **kw: events.append((action, skill_id, kw))))
    sm = _FakeSM()
    cur = _curator(usage, sm, now=1_000_000.0 + 2 * DAY)
    monkeypatch.setattr(cur, "_recent_runs", lambda uid, since: _worse_runs())
    lines = cur.contribution_report()
    assert lines == ["s: 1/6 useful when loaded vs 6/6 without — patch or archive?"]
    assert [(a, kw["reason"]) for a, _s, kw in events] == [("propose", "contribution")]
    assert sm.archived == []
    assert cur.contribution_report() == []  # once per window
    assert len(events) == 1


def test_contribution_report_unreadable_runs_files_nothing(usage, monkeypatch):
    usage.record_provenance("s", "u1", "agent")
    cur = _curator(usage, _FakeSM(), now=1_000_000.0)

    def boom(uid, since):
        raise RuntimeError("episode store down")
    monkeypatch.setattr(cur, "_recent_runs", boom)
    assert cur.contribution_report() == []
