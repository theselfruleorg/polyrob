"""108 W5: payload.expect, checked by code against this run's own act rows."""
import asyncio
import types

import pytest

from agents.task.agent.core import curator
from agents.task.agent.core import episodic_digest as ED
from core import cron_expect as CE
from core.event_log import get_event_log


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    return get_event_log()


def _act(log, effect="social", sid="s1", user="u1", outcome="ok"):
    log.record("external_write", user_id=user, session_id=sid, effect=effect,
               attrs={"outcome": outcome})


def _job(expect):
    return types.SimpleNamespace(id="j1", user_id="u1", payload={"expect": expect} if expect else {})


def _verified(expect, outcome="done", sid="s1"):
    return CE.verified_attrs(_job(expect), outcome, sid)


def test_expect_met(log):
    _act(log)
    assert _verified({"effect": "social", "min": 1}) == {"verified": True}


def test_expect_not_met_reads_only_good_rows_of_this_run_and_this_effect(log):
    _act(log, outcome="error")
    _act(log, effect="comms")
    _act(log, sid="another-run")
    _act(log, user="another-tenant")
    assert _verified({"effect": "social", "min": 1}) == {"verified": False}
    _act(log)
    assert _verified({"effect": "social", "min": 2}) == {"verified": False}
    _act(log)
    assert _verified({"effect": "social", "min": 2}) == {"verified": True}


def test_idle_ok_needs_a_clean_end_and_no_act(log):
    assert _verified({"idle_ok": True}) == {"verified": True}
    assert _verified({"idle_ok": True}, outcome="skipped") == {"verified": True}
    assert _verified({"idle_ok": True}, outcome="failed") == {"verified": False}


def test_no_expect_leaves_verified_empty(log):
    assert _verified(None) == {}
    assert _verified({"effect": "social"}, outcome="started") == {}
    assert _verified({"effect": "social"}, sid=None) == {}


@pytest.mark.parametrize("off", ["rows_none", "raises"])
def test_an_unreadable_log_is_not_a_failed_run(log, monkeypatch, off):
    def rows(self, **kw):
        if off == "raises":
            raise OSError("disk")
    monkeypatch.setattr(type(log), "rows_where", rows)
    assert _verified({"effect": "social"}) == {}


def test_telemetry_off_is_not_a_failed_run(log, monkeypatch):
    monkeypatch.setattr("core.effects.external_write_telemetry_enabled", lambda: False)
    assert _verified({"effect": "social"}) == {}


@pytest.mark.parametrize("bad", [
    {"effect": "nonsense"}, {"min": 1}, {"effect": "social", "min": 0},
    {"effect": "social", "min": True}, {"effect": "social", "min": "1"},
    {"effect": "social", "idle_ok": True}, {"idle_ok": False}, {"effect": "social", "x": 1},
    ["social"],
])
def test_validate_refuses(bad):
    with pytest.raises(ValueError):
        CE.validate_expect(bad)


def test_validate_stores_one_form():
    assert CE.validate_expect({"effect": "social"}) == {"effect": "social", "min": 1}
    assert CE.validate_expect({"idle_ok": True}) == {"idle_ok": True}
    assert CE.validate_expect({}) is None


def test_every_reader_takes_the_bool(monkeypatch):
    assert ED.signal_source({"verified": False}, useful=False) == "judge"
    assert ED.signal_source({"verified": True}, useful=False) == "judge"
    assert ED.signal_source({"verified": "unverified"}, useful=False) == "judge"
    assert ED.signal_source({}, useful=False) == "none"
    assert curator._run_good({"verified": True}) is True
    assert curator._run_good({"verified": "verified"}) is True
    assert curator._run_good({"verified": False, "useful": True}) is False
    assert curator._run_good({"verified": "unverified", "useful": True}) is True
    head = ED._track_record(types.SimpleNamespace(spend_usd=0), {"verified": False})
    assert head.startswith("verified=no ")


def test_lane_rates_count_the_bool(monkeypatch):
    rows = [types.SimpleNamespace(meta={"lane": "a", "verified": v, "job_id": "j1"},
                                  thread_key="cron:j1", goal_id=None, kind="cron")
            for v in (True, False, "verified", "unverified")] + [
        types.SimpleNamespace(meta={"lane": "a", "job_id": "j1"}, thread_key="cron:j1",
                              goal_id=None, kind="cron")]

    async def recall(**kw):
        return rows if kw.get("kind") == "cron" else []
    monkeypatch.setattr("modules.memory.registry.memory_recall_episodes", recall)
    rates = asyncio.run(ED.lane_outcome_rates(user_id="u1"))
    assert (rates[0]["verified"], rates[0]["verified_n"]) == (2, 4)


# --- the runner's two writes ------------------------------------------------------------

def test_the_cron_run_row_carries_verified_only_with_expect(log):
    from cron.runner import _cron_ev
    _act(log)
    _cron_ev(_job({"effect": "social", "min": 1}), "done", session_id="s1")
    _cron_ev(_job(None), "done", session_id="s1")
    _cron_ev(_job({"effect": "social", "min": 1}), "started", session_id="s1")
    rows = {(r["attrs"].get("outcome"), r["attrs"].get("verified"))
            for r in log.query(kind="cron_run", limit=9)}
    assert rows == {("done", True), ("done", None), ("started", None)}


def test_the_run_record_takes_verified_through_meta(monkeypatch):
    seen = {}

    async def finalize(**kw):
        seen.update(kw)
    monkeypatch.setattr("modules.memory.episodic.finalize_episode", finalize)
    monkeypatch.setattr(ED, "rail_lane", lambda rail, data_dir=None: "")
    run = types.SimpleNamespace(done_text="", user_messages=[], artifacts=[], evidence=None,
                                spend_usd=0.0, steps=1, skills_loaded=[], stop_reason="done")
    asyncio.run(ED.record_run(regime="cron", kind="cron", session_id="s1", user_id="u1",
                              rail_id="cron:j1", task="t", outcome="done", run=run,
                              meta={"verified": False}))
    assert seen["meta"]["verified"] is False and seen["meta"]["signal_source"] == "judge"


# --- the tool ---------------------------------------------------------------------------

def _tool(tmp_path):
    from cron.jobs import CronJobStore
    from cron.service import CronService
    from tools.cronjob_tools import CronJobTool
    tool = object.__new__(CronJobTool)
    tool._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return tool


TURN = types.SimpleNamespace(user_id="alice", role="orchestrator", is_sub_agent=False,
                             metadata={"owner_text": "set it up"}, session_id=None,
                             parent_session_id=None)


def test_the_tool_sets_shows_replaces_and_clears(tmp_path):
    from tools.cronjob_tools import CronEditAction, CronScheduleAction, CronShowAction
    tool = _tool(tmp_path)
    store = tool._cron_service.store
    res = asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task="post the weekly note", schedule="1d",
                           expect={"effect": "social"}), execution_context=TURN))
    assert res.error is None, res.error
    job = store.list(user_id="alice")[0]
    assert job.payload["expect"] == {"effect": "social", "min": 1}
    shown = asyncio.run(tool.cronjob_show(CronShowAction(job_id=job.id), execution_context=TURN))
    assert "expect: at least 1 social act(s) per run" in shown.extracted_content

    res = asyncio.run(tool.cronjob_edit(CronEditAction(job_id=job.id, expect={"idle_ok": True}),
                                        execution_context=TURN))
    assert res.error is None, res.error
    assert store.get(job.id).payload["expect"] == {"idle_ok": True}
    res = asyncio.run(tool.cronjob_edit(CronEditAction(job_id=job.id, expect={}),
                                        execution_context=TURN))
    assert res.error is None, res.error
    assert "expect" not in store.get(job.id).payload


def test_the_tool_refuses_a_bad_expect_and_a_job_with_no_model_run(tmp_path):
    from tools.cronjob_tools import CronEditAction, CronScheduleAction
    tool = _tool(tmp_path)
    res = asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task="post the weekly note", schedule="1d",
                           expect={"effect": "nonsense"}), execution_context=TURN))
    assert "expect.effect" in res.error
    res = asyncio.run(tool.cronjob_schedule(
        CronScheduleAction(task="post the weekly note", schedule="1d", wake_agent=False,
                           expect={"idle_ok": True}), execution_context=TURN))
    assert "needs a model run" in res.error
    job = tool._cron_service.schedule(task="post the weekly note", schedule_spec="1d",
                                      user_id="alice", payload={"wake_agent": False})
    res = asyncio.run(tool.cronjob_edit(CronEditAction(job_id=job.id, expect={"idle_ok": True}),
                                        execution_context=TURN))
    assert "needs a model run" in res.error


# --- 116 W2.2: run_effect, the one label for what a finished run did ---------

def test_run_effect_labels():
    from core.cron_expect import run_effect
    assert run_effect({"useful": True}) == "effect"
    assert run_effect({"useful": True, "verified": True}) == "effect"
    assert run_effect({"useful": False, "verified": True}) == "idle"
    assert run_effect({"useful": True, "verified": False}) == "missed"
    assert run_effect({"useful": False}) == "unverified"
    assert run_effect({}) == "unverified"
    assert run_effect(None) == "unverified"
    assert run_effect({"useful": True, "all_errored_verdict": True}) == "failed:all_errored"


def test_run_effect_reads_a_goal_word():
    from core.cron_expect import run_effect
    assert run_effect({"verified": "verified"}) == "idle"
    assert run_effect({"verified": "unverified"}) == "missed"
    assert run_effect({"verified": ""}) == "unverified"


def test_run_effect_returns_only_the_five_labels():
    from core.cron_expect import RUN_EFFECTS, run_effect
    cases = [{}, {"useful": True}, {"verified": False}, {"verified": True},
             {"all_errored_verdict": True}, {"useful": "yes"}, {"verified": 1}]
    assert {run_effect(c) for c in cases} <= set(RUN_EFFECTS)
