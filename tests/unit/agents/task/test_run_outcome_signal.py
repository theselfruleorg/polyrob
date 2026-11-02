"""095 W11: the outcome signal on the run record, end to end.

The record carries ``useful`` (the ONE ``did_something`` predicate) and the
``signal_source`` it can be trusted by (receipt > owner > judge > self > none);
``<previous-runs>`` renders one track-record line per run; the loaded skills
carry their patch generation; insights reads the per-lane rates."""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from agents.task.agent.core import episodic_digest as ED
from modules.memory.provider import EpisodeRecord
from modules.skills.skill_usage import SkillUsageStore


def _run(**kw):
    base = dict(done_text="", user_messages=[], artifacts=[], evidence=None,
                spend_usd=0.25, steps=4, skills_loaded=[], stop_reason="done")
    base.update(kw)
    return SimpleNamespace(**base)


def _record(monkeypatch, run, *, meta=None, summary=None, regime="cron", kind="cron"):
    seen = {}

    async def finalize(**kw):
        seen.update(kw)
    monkeypatch.setattr("modules.memory.episodic.finalize_episode", finalize)
    monkeypatch.setattr(ED, "rail_lane", lambda rail, data_dir=None: "lane-a")
    asyncio.run(ED.record_run(regime=regime, kind=kind, session_id="s1", user_id="u1",
                              rail_id=f"{kind}:j1", task="t", outcome="done", run=run,
                              summary=summary, meta=meta))
    return seen["meta"]


def test_episode_meta_carries_signal_source(monkeypatch):
    sent = _run(user_messages=["posted the weekly note"])
    m = _record(monkeypatch, sent)
    assert m["useful"] is True and m["signal_source"] == "receipt"
    assert m["stop_reason"] == "done" and m["lane"] == "lane-a"

    m = _record(monkeypatch, _run(), meta={"verified": "unverified"},
                regime="goal", kind="goal")
    assert m["useful"] is False and m["signal_source"] == "judge"

    m = _record(monkeypatch, _run(), meta={"owner_verdict": "good", "verified": "verified"})
    assert m["signal_source"] == "owner"

    m = _record(monkeypatch, _run(done_text="I think it went well"))
    assert m["signal_source"] == "self"

    assert ED.signal_source({}, useful=False, run=_run(), summary="") == "none"


def test_skills_loaded_carry_their_patch_generation(monkeypatch, tmp_path):
    store = SkillUsageStore(str(tmp_path / "skill_usage.db"))
    store.record_change("lane-skill", "u1", action="promote", actor="user")
    store.record_change("lane-skill", "u1", action="promote", actor="user")
    monkeypatch.setattr("modules.skills.skill_usage.existing_skill_usage_store",
                        lambda: store)
    m = _record(monkeypatch, _run(skills_loaded=["lane-skill", "other"]))
    assert m["skill_gens"] == {"lane-skill": 2, "other": 0}


def test_previous_runs_line_is_the_track_record():
    e = EpisodeRecord(ts=1_760_000_000, user_id="u1", session_id="s1", kind="goal",
                      outcome="done", summary="Posted.", spend_usd=0.5,
                      meta={"verified": "verified", "useful": True, "stop_reason": "done",
                            "skills_loaded": ["lane-skill", "other"],
                            "skill_gens": {"lane-skill": 2, "other": 0}})
    line = ED._render_run(e)
    assert ("DONE verified=verified useful=yes stop=done "
            "skills=lane-skill@g2,other $0.50: Posted.") in line


def test_failed_run_shows_its_lesson_not_its_transcript():
    e = EpisodeRecord(ts=1_760_000_000, user_id="u1", session_id="s1", kind="cron",
                      outcome="failed", summary="FAILED: x " + "y" * 2000,
                      meta={"useful": False, "errors": ["send_email: 535 auth failed"]})
    line = ED._render_run(e)
    assert "useful=no" in line and "error: send_email: 535 auth failed" in line
    assert len(line) < 700


def test_skill_usage_patch_generation_migration(tmp_path):
    db = tmp_path / "skill_usage.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE skill_usage (skill_id TEXT NOT NULL, user_id TEXT NOT NULL,"
        " load_count INTEGER NOT NULL DEFAULT 0, last_used_at REAL,"
        " PRIMARY KEY (skill_id, user_id));"
        "INSERT INTO skill_usage VALUES ('s','u1',3,1.0);")
    conn.commit()
    conn.close()
    store = SkillUsageStore(str(db))
    SkillUsageStore(str(db))  # idempotent: a second open adds nothing
    row = store.get_usage("s", "u1")
    assert row["load_count"] == 3 and row["patch_generation"] == 0
    store.record_change("s", "u1", action="promote", actor="user")
    store.record_change("s", "u1", action="promote", actor="user", pending=True)
    store.record_change("s", "u1", action="edit", actor="agent")
    row = store.get_usage("s", "u1")
    assert row["patch_generation"] == 1 and row["last_patch_at"] is not None
    assert row["load_count"] == 3


@pytest.mark.asyncio
async def test_lane_rates_count_only_rows_that_carry_the_field(monkeypatch):
    rows = [
        EpisodeRecord(ts=1, user_id="u1", session_id="a", kind="cron", outcome="done",
                      thread_key="cron:j1", meta={"lane": "lane-a", "useful": True}),
        EpisodeRecord(ts=2, user_id="u1", session_id="b", kind="cron", outcome="failed",
                      thread_key="cron:j1", meta={"lane": "lane-a", "useful": False}),
        EpisodeRecord(ts=3, user_id="u1", session_id="c", kind="cron", outcome="done",
                      thread_key="cron:j1", meta={"lane": "lane-a"}),  # legacy row
        EpisodeRecord(ts=4, user_id="u1", session_id="d", kind="goal", outcome="done",
                      thread_key="goal:g1", meta={"lane": "lane-b", "useful": False,
                                                  "verified": "verified"}),
    ]

    async def recall(**kw):
        return [r for r in rows if r.kind == kw.get("kind")]
    monkeypatch.setattr("modules.memory.registry.memory_recall_episodes", recall)
    rates = await ED.lane_outcome_rates(user_id="u1")
    by = {d["lane"]: d for d in rates}
    assert by["lane-a"]["runs"] == 3 and (by["lane-a"]["useful"], by["lane-a"]["useful_n"]) == (1, 2)
    assert (by["lane-b"]["verified"], by["lane-b"]["verified_n"]) == (1, 1)
    text = ED.render_lane_rates(rates)
    assert "lane-a: 3 runs · useful 1/2" in text and "verified 1/1" in text
