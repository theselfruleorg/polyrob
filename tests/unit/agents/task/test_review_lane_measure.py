"""108 W6: the review prompt carries each lane skill; a mechanism fault reaches the digest; a
correction starts one review; the curator compares each live change with its lane, and on harm
the rollback runs once."""
import asyncio
import json
import types

import pytest

import agents.task.agent.core.background_review as br
import agents.task.agent.core.lane_measure as LM
from agents.task.agent.core.curator import SkillCurator, contribution_line
from agents.task.agent.skill_manager import SkillManager
from modules.skills.skill_usage import SkillUsageStore, get_skill_usage_store, reset_skill_usage_store

DAY = 86400.0
NOW = 2_000_000.0
BODY = "---\nname: lane-skill\ndescription: a lane skill\n---\n# Lane skill\n\nStep one.\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    reset_skill_usage_store()
    yield tmp_path
    reset_skill_usage_store()


@pytest.fixture
def sm(home):
    return SkillManager(str(home / "skills"))


def _run(ts, *, lane="lane-a", steps=4, spend=0.01, **meta):
    outcome = meta.pop("outcome", "done")
    return types.SimpleNamespace(ts=int(ts), steps=steps, spend_usd=spend, outcome=outcome,
                                 thread_key="cron:j1", meta={"lane": lane, **meta})


def _usage(home):
    return SkillUsageStore(str(home / "usage.db"), clock=lambda: NOW)


def _row(usage, *, before=None, after_sha="sha-after", before_sha="sha-before", ts=NOW - DAY):
    row = {"state": "open", "user_id": "u1", "skill_id": "lane-skill", "lane": "lane-a",
           "rail": "cron:j1", "signal": "owner_correct", "ts": ts, "before_sha": before_sha,
           "after_sha": after_sha,
           "before": before if before is not None else {"runs": 10, "steps": 40, "errors": 1,
                                                        "cost": 0.1, "corrections": 0}}
    key = LM.row_key("u1", "lane-skill", after_sha)
    usage.set_state(key, json.dumps(row))
    return key


def _stored(usage, key):
    return json.loads(usage.get_state(key))


class _SM:
    """A skill whose live body is the change's, until a rollback replaces it."""

    def __init__(self, live="sha-after", ok=True):
        self.live, self.ok, self.rollbacks, self.archived = live, ok, [], []

    def active_skill_text(self, skill_id, *, user_id):
        return self.live

    @staticmethod
    def _content_revision(text):
        return text

    def rollback_skill(self, skill_id, *, user_id, to_sha=None, created_by=""):
        self.rollbacks.append((skill_id, user_id, to_sha, created_by))
        if self.ok:
            self.live = to_sha
        return self.ok

    def delete_skill(self, skill_id, *, user_id, created_by="", aged=False, **_kw):
        self.archived.append((skill_id, created_by, aged))
        return True


def _curator(usage, sm, runs, monkeypatch, *, now=NOW, dry_run=False):
    cur = SkillCurator(sm, usage, clock=lambda: now, dry_run=dry_run)
    monkeypatch.setattr(cur, "_recent_runs", lambda user_id, since: list(runs))
    events = []
    monkeypatch.setattr(SkillCurator, "_self_mod_ev",
                        staticmethod(lambda action, sid, uid, **kw: events.append((action, sid, kw))))
    return cur, events


# --- 1. the prompt holds the body and the sha256 ---------------------------------------

def test_prompt_gives_each_lane_skill_body_and_the_sha_a_patch_passes(sm):
    assert sm.create_skill("lane-skill", BODY, user_id="u1", created_by="agent").ok
    blocks = br.lane_skill_blocks("u1", ["lane-skill", "lane-skill", "no-such-skill"],
                                  "background_review")
    sha = sm._content_revision(BODY)
    assert f'<skill id="lane-skill" sha256="{sha}">' in blocks and "Step one." in blocks
    assert blocks.count("<skill ") == 1
    assert sm.patch_skill("lane-skill", user_id="u1", old_string="Step one.",
                          new_string="Step one.\nStep two.", created_by="background_review",
                          expected_revision=sha).ok
    prompt = br.build_run_review_prompt("", outcome_line="outcome=done", lane_skills=blocks)
    assert "LANE SKILLS:" in prompt and "Step one." in prompt


def test_a_long_body_is_cut_and_the_prompt_says_so(sm, monkeypatch):
    monkeypatch.setattr(br, "_LANE_BODY_MAX_CHARS", 40)
    assert sm.create_skill("lane-skill", BODY, user_id="u1", created_by="agent").ok
    assert "the body is cut here" in br.lane_skill_blocks("u1", ["lane-skill"], "agent")


# --- 3. a mechanism fault; 4. one change, one row ------------------------------------------

def test_the_reviewer_patch_row_is_the_owner_line(monkeypatch, home):
    monkeypatch.setenv("EVENT_LOG_ENABLED", "true")
    from core import recap
    attrs = {"kind": "skill", "action": "patch", "item_id": "lane-skill", "ok": True,
             "pending": False, "created_by": "background_review",
             "note": "run=cron:j1 signal=owner_correct: the run skipped a step"}
    line = recap.self_modification_text(attrs)
    assert "run=cron:j1 signal=owner_correct" in line and "roll it back" in line
    log = types.SimpleNamespace(query=lambda **kw: [{"attrs": attrs}, {"attrs": dict(attrs, pending=True)},
                                                    {"attrs": {"action": "patch", "created_by": "agent",
                                                               "note": "n"}}])
    import core.event_log as EL
    monkeypatch.setattr(EL, "event_log_enabled", lambda: True)
    monkeypatch.setattr(EL, "open_event_log", lambda home=None: log)
    assert recap.curator_proposals("u1", 0.0) == [line]


def test_a_mechanism_fault_reaches_the_digest_and_writes_no_skill(monkeypatch, home):
    recorded = []
    import core.event_log as EL
    monkeypatch.setattr(EL, "event_log_enabled", lambda: True)
    monkeypatch.setattr(EL, "get_event_log", lambda: types.SimpleNamespace(
        record=lambda kind, **kw: recorded.append((kind, kw))))
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    monkeypatch.setattr(br, "_drafts_since", lambda user_id, since_ts: [])
    output = types.SimpleNamespace(final_text="The fetch tool timed out.\nMECHANISM: the fetch tool  times out")

    class _Mgr:
        calls = 0

        async def run_subtask(self, **kw):
            _Mgr.calls += 1
            assert "NO CHANGE IS THE DEFAULT" in kw["task"] and "MECHANISM:" in kw["task"]
            return types.SimpleNamespace(output=output)

    agent = types.SimpleNamespace(_is_sub_agent=False, agent_id="main", llm="M", _judge_llm="A",
                                  history=types.SimpleNamespace(history=[]))
    orch = types.SimpleNamespace(agents={"main": agent}, get_sub_agent_manager=lambda: _Mgr())
    task_agent = types.SimpleNamespace(get_orchestrator=lambda sid: orch)
    from agents.task.session_class import CRON
    assert asyncio.run(br.review_after_run(task_agent, regime=CRON, session_id="s1", user_id="u1",
                                           rail_id="cron:j1", outcome="failed",
                                           meta={"error": "boom"})) is True
    ((kind, kw),) = recorded
    assert kind == "review_mechanism_note" and kw["attrs"]["note"] == "the fetch tool times out"

    from core import recap
    from cron import digest as D
    log = types.SimpleNamespace(query=lambda **kw: [{"attrs": {"note": "the fetch tool times out"}}] * 2)
    monkeypatch.setattr(EL, "open_event_log", lambda home=None: log)
    assert recap.mechanism_notes("u1", 0.0) == ["the fetch tool times out"]
    assert D._mechanism_notes("u1", 0.0, None) == ["the fetch tool times out"]


# --- a correction reaches the reviewer, once per run ---------------------------------------

def test_a_correction_starts_one_review(monkeypatch):
    from agents.task.agent.core import owner_verdict as OV
    import modules.memory.registry as R
    episode = types.SimpleNamespace(meta={"useful": False, "lane": "lane-a"}, summary="s", ts=1,
                                    session_id="s1")

    async def _recall(**kw):
        return [episode]

    async def _update(**kw):
        episode.meta = {**episode.meta, **kw["patch"]}
        return True

    monkeypatch.setattr(R, "memory_recall_episodes", _recall)
    monkeypatch.setattr(R, "memory_update_episode_meta", _update)
    fired = []
    hook = lambda ep, meta: fired.append(meta["owner_verdict"])
    asyncio.run(OV.write_verdict("u1", "cron:j1", "correct", "wrong", on_correct=hook))
    asyncio.run(OV.write_verdict("u1", "cron:j1", "correct", "wrong again", on_correct=hook))
    asyncio.run(OV.write_verdict("u1", "cron:j1", "confirm", "fine", on_correct=hook))
    assert fired == ["correct"]


def test_review_after_verdict_uses_the_record_and_the_owner_session(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    monkeypatch.setattr(br, "_drafts_since", lambda user_id, since_ts: [])
    monkeypatch.setattr(br, "lane_skill_blocks", lambda *a, **k: "")

    async def _no_measure(*a, **k):
        return 0
    import modules.skills.skill_usage as SU
    monkeypatch.setattr(LM, "record_changes", _no_measure)
    monkeypatch.setattr(SU, "get_skill_usage_store", lambda *a, **k: None)
    calls = []

    class _Mgr:
        async def run_subtask(self, **kw):
            calls.append(kw)

    agent = types.SimpleNamespace(agent_id="owner", llm="M", _judge_llm="A")
    orch = types.SimpleNamespace(get_sub_agent_manager=lambda: _Mgr())
    episode = types.SimpleNamespace(kind="cron", user_id="u1", session_id="s1", outcome="done",
                                    task="post the note", summary="posted")
    meta = {"owner_verdict": "correct", "owner_verdict_quote": "wrong channel"}
    assert asyncio.run(br.review_after_verdict(agent, orch, episode=episode, rail_id="cron:j1",
                                               meta=meta)) is True
    assert "wrong channel" in calls[0]["task"] and "owner_verdict=correct" in calls[0]["task"]


# --- 5. the lane measure -------------------------------------------------------------------

def test_window_counts_steps_errors_cost_and_corrections():
    w = LM.window([_run(1, steps=3, spend=0.5), _run(2, steps=5, spend=0.25, stop_reason="max_steps"),
                   _run(3, steps=0, spend=0.0, owner_verdict="correct"), _run(4, outcome="failed")])
    assert w == {"runs": 4, "steps": 12, "errors": 2, "cost": 0.76, "corrections": 1}


def test_each_live_change_stores_lane_and_before_window(home, monkeypatch):
    usage = _usage(home)
    change_ts = NOW
    usage.record_change("lane-skill", "u1", action="patch", actor="background_review",
                        before_sha="sha-b", after_sha="sha-a")
    usage.record_change("lane-skill", "u1", action="patch", actor="agent", after_sha="sha-x")
    usage.record_change("draft-skill", "u1", action="patch", actor="background_review",
                        after_sha="sha-d", pending=True)

    async def _runs(user_id, since_ts):
        return [_run(change_ts - DAY, steps=2), _run(change_ts - 2 * DAY, steps=4, stop_reason="loop"),
                _run(change_ts - DAY, lane="lane-b", steps=99), _run(change_ts + 1, steps=50)]

    monkeypatch.setattr(LM, "_read_runs", _runs)
    n = asyncio.run(LM.record_changes(usage, user_id="u1", since_ts=NOW - 1, rail_id="cron:j1",
                                      signal="owner_correct", meta={"lane": "lane-a"}))
    assert n == 1
    (raw,) = usage.list_state(LM.KEY_PREFIX).values()
    row = json.loads(raw)
    assert row["state"] == "open" and row["lane"] == "lane-a" and row["signal"] == "owner_correct"
    assert row["before_sha"] == "sha-b" and row["after_sha"] == "sha-a"
    assert row["before"] == {"runs": 2, "steps": 6, "errors": 1, "cost": 0.02, "corrections": 0}


def test_an_unreadable_run_store_is_not_a_zero_window(home, monkeypatch):
    usage = _usage(home)
    usage.record_change("lane-skill", "u1", action="patch", actor="background_review",
                        before_sha="b", after_sha="a")

    async def _boom(user_id, since_ts):
        raise RuntimeError("store down")

    monkeypatch.setattr(LM, "_read_runs", _boom)
    asyncio.run(LM.record_changes(usage, user_id="u1", since_ts=NOW - 1, rail_id="cron:j1",
                                  signal="run_error", meta={}))
    (raw,) = usage.list_state(LM.KEY_PREFIX).values()
    row = json.loads(raw)
    assert row["before"] is None and row["lane"] == "cron:j1"  # no lane: the rail is the lane


def test_not_due_before_ten_runs_or_fourteen_days(home, monkeypatch):
    usage = _usage(home)
    key = _row(usage)
    cur, events = _curator(usage, _SM(), [_run(NOW - 10)] * 3, monkeypatch)
    assert cur.measure_changes() == [] and events == []
    assert _stored(usage, key)["state"] == "open"


def test_a_lane_under_ten_runs_reads_unknown_and_decides_nothing(home, monkeypatch):
    """Fourteen days passed and every run failed: the rate is no test on three runs."""
    usage = _usage(home)
    key = _row(usage, ts=NOW - 15 * DAY)
    sm = _SM()
    cur, events = _curator(usage, sm, [_run(NOW - 10, stop_reason="max_steps")] * 3, monkeypatch)
    (line,) = cur.measure_changes()
    assert "reads unknown" in line and "3 lane run(s)" in line
    assert line == contribution_line(LM.finding(_stored(usage, key), _stored(usage, key)["after"], "unknown"))
    assert sm.rollbacks == []
    assert _stored(usage, key)["state"] == "unknown"
    assert [e[0] for e in events] == ["measure"]
    assert cur.measure_changes() == []  # one line per change


def test_no_before_window_reads_unknown(home, monkeypatch):
    usage = _usage(home)
    key = _row(usage, before={"runs": 0, "steps": 0, "errors": 0, "cost": 0, "corrections": 0})
    cur, _ = _curator(usage, _SM(), [_run(NOW - 10, stop_reason="max_steps")] * 10, monkeypatch)
    assert "reads unknown" in cur.measure_changes()[0]
    assert _stored(usage, key)["state"] == "unknown"


def test_ten_runs_with_no_harm_report_through_contribution_line(home, monkeypatch):
    usage = _usage(home)
    key = _row(usage)
    sm = _SM()
    cur, events = _curator(usage, sm, [_run(NOW - 10, steps=5)] * 10, monkeypatch)
    (line,) = cur.measure_changes()
    assert "errors 0/10 vs 1/10" in line and "steps 5.0 vs 4.0" in line and "corrections 0 vs 0" in line
    assert _stored(usage, key)["state"] == "kept" and sm.rollbacks == []
    assert events[0][0] == "measure" and events[0][2]["note"] == line


def test_an_old_run_does_not_count_after_the_change(home, monkeypatch):
    usage = _usage(home)
    key = _row(usage)
    cur, _ = _curator(usage, _SM(), [_run(NOW - 2 * DAY, owner_verdict="correct")] * 10, monkeypatch)
    assert cur.measure_changes() == []
    assert _stored(usage, key)["state"] == "open"


# --- 6. harm: the rollback runs once --------------------------------------------------------

def test_a_new_correction_rolls_back_once_with_one_owner_line(home, monkeypatch):
    usage = _usage(home)
    key = _row(usage)
    sm = _SM()
    cur, events = _curator(usage, sm, [_run(NOW - 10, owner_verdict="correct")], monkeypatch)
    (line,) = cur.measure_changes()
    assert "rolled back" in line and "1 new owner correction" in line and "archive keeps it" in line
    assert sm.rollbacks == [("lane-skill", "u1", "sha-before", "curator")]
    assert _stored(usage, key)["state"] == "rolled_back"
    assert [e[0] for e in events] == ["rollback"] and events[0][2]["note"] == line
    assert cur.measure_changes() == []
    assert len(sm.rollbacks) == 1 and len(events) == 1


def test_a_failed_rate_that_doubles_rolls_back(home, monkeypatch):
    usage = _usage(home)
    key = _row(usage)  # before: 1 error in 10 runs
    sm = _SM()
    runs = [_run(NOW - 10, stop_reason="max_steps")] * 2 + [_run(NOW - 10)] * 8
    cur, _ = _curator(usage, sm, runs, monkeypatch)
    assert "error rate doubled" in cur.measure_changes()[0]
    assert _stored(usage, key)["harm"] == "harm_errors" and len(sm.rollbacks) == 1


@pytest.mark.parametrize("errors, doubled", [(1, False), (2, True)])
def test_one_error_is_no_doubling_of_a_zero_rate(errors, doubled):
    before = {"runs": 10, "errors": 0}
    assert LM.error_rate_doubled(before, {"runs": 10, "errors": errors}) is doubled


def test_a_later_write_ends_the_row_with_no_rollback(home, monkeypatch):
    usage = _usage(home)
    key = _row(usage)
    sm = _SM(live="sha-newer")
    cur, events = _curator(usage, sm, [_run(NOW - 10, owner_verdict="correct")], monkeypatch)
    assert cur.measure_changes() == []
    assert sm.rollbacks == [] and events == []
    assert _stored(usage, key)["state"] == "superseded"


def test_a_failed_rollback_tells_the_owner_and_does_not_retry(home, monkeypatch):
    usage = _usage(home)
    key = _row(usage)
    sm = _SM(ok=False)
    cur, events = _curator(usage, sm, [_run(NOW - 10, owner_verdict="correct")], monkeypatch)
    assert "rollback failed" in cur.measure_changes()[0]
    assert _stored(usage, key)["state"] == "rollback_failed"
    assert events[0][2]["ok"] is False
    cur.measure_changes()
    assert len(sm.rollbacks) == 1


def test_a_created_skill_moves_to_the_archive(home, monkeypatch):
    usage = _usage(home)
    _row(usage, before_sha="")
    sm = _SM()
    cur, _ = _curator(usage, sm, [_run(NOW - 10, owner_verdict="correct")], monkeypatch)
    cur.measure_changes()
    assert sm.rollbacks == [] and sm.archived == [("lane-skill", "curator", True)]


def test_dry_run_changes_nothing(home, monkeypatch):
    usage = _usage(home)
    key = _row(usage)
    sm = _SM()
    cur, events = _curator(usage, sm, [_run(NOW - 10, owner_verdict="correct")], monkeypatch, dry_run=True)
    assert len(cur.measure_changes()) == 1
    assert sm.rollbacks == [] and events == [] and _stored(usage, key)["state"] == "open"


def test_unreadable_runs_leave_the_row_open(home, monkeypatch):
    usage = _usage(home)
    key = _row(usage)
    cur = SkillCurator(_SM(), usage, clock=lambda: NOW)

    def _boom(user_id, since):
        raise RuntimeError("store down")

    monkeypatch.setattr(cur, "_recent_runs", _boom)
    assert cur.measure_changes() == []
    assert _stored(usage, key)["state"] == "open"


def test_the_machine_refuses_a_second_result():
    from core.state.machine import IllegalTransition
    assert LM.CHANGE_MEASURE.allowed("open", "rolled_back")
    with pytest.raises(IllegalTransition):
        LM.CHANGE_MEASURE.check("rolled_back", "rolled_back")


def test_real_rollback_restores_the_body_and_the_archive_keeps_the_change(sm, home):
    assert sm.create_skill("lane-skill", BODY, user_id="u1", created_by="agent").ok
    before = sm._content_revision(BODY)
    res = sm.patch_skill("lane-skill", user_id="u1", old_string="Step one.", new_string="Step bad.",
                         created_by="background_review", expected_revision=before)
    assert res.ok
    usage = get_skill_usage_store()
    (change,) = usage.live_changes_since("u1", 0, actor="background_review")
    assert change["before_sha"] == before and change["after_sha"] == res.revision
    row = {"skill_id": "lane-skill", "user_id": "u1", "before_sha": before, "after_sha": res.revision}
    assert LM.live_revision(sm, row) == res.revision
    assert LM.roll_back(sm, row) is True
    assert LM.live_revision(sm, row) == before
    assert res.revision in sm._archived_bodies("u1", "lane-skill")
    assert "Step bad." in sm._archived_bodies("u1", "lane-skill")[res.revision]
    # A second run has nothing to undo: the live body is no longer the change's.
    assert LM.live_revision(sm, row) != row["after_sha"]
