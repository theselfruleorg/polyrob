"""The collection revealer as a deterministic WRITE job —
exactly one allowlisted verb (agent_nft.agent_nft_collection_reveal), no model turn, OFF behind
CRON_WRITE_JOBS_ENABLED, owner-authored jobs only, held by the owner pause and pause_windows."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from cron import write_job as W
from cron.jobs import CronJob, CronJobStore
from cron.schedule import ScheduleError
from cron.service import CronService

SPEC = {"verb": "agent_nft.agent_nft_collection_reveal", "params": {"chain": "robinhood", "max_ids": 10, "dry_run": False}}


def test_the_allowlist_is_exactly_the_revealer():
    assert set(W.WRITE_VERBS) == {"agent_nft.agent_nft_collection_reveal"}
    assert set(W.SPENDS) == set(W.WRITE_VERBS)


def test_validate_refuses_other_verbs_bad_params_and_bad_delivery():
    for verb in ("agent_nft.agent_nft_collection_mint", "agent_nft.agent_nft_withdraw_token",
                 "defi_trade.swap", "defi_data.pool_metrics",
                 "agent_nft.reveal"):          # no alias of the verb
        with pytest.raises(W.WriteJobError, match="not an allowlisted write verb"):
            W.validate_write_verb({"verb": verb, "params": {}})
    with pytest.raises(W.WriteJobError, match="do not validate"):
        W.validate_write_verb({"verb": "agent_nft.agent_nft_collection_reveal", "params": {"max_ids": 500}})
    with pytest.raises(W.WriteJobError, match="do not validate"):
        W.validate_write_verb({"verb": "agent_nft.agent_nft_collection_reveal", "params": {"chain": "base"}})
    with pytest.raises(W.WriteJobError, match="deliver_on"):
        W.validate_write_verb(dict(SPEC, deliver_on="sometimes"))
    assert W.validate_write_verb(SPEC)["deliver_on"] == "alert"


class _Tool:
    text, error, calls = "COLLECTION REVEAL … revealed 3", None, []

    async def agent_nft_collection_reveal(self, params, execution_context="unset"):
        _Tool.calls.append((params, execution_context))
        return SimpleNamespace(extracted_content=self.text, error=self.error)


@pytest.fixture
def delivered(monkeypatch):
    sent = []

    async def deliver(task_agent, job, text, *, target, deliver_target=None):
        sent.append((text, target))
        return True
    monkeypatch.setattr("cron.delivery.deliver_result", deliver)
    monkeypatch.setenv(W.FLAG, "true")
    _Tool.calls, _Tool.text, _Tool.error = [], "COLLECTION REVEAL … revealed 3", None
    return sent


def _job(payload):
    return CronJob(id="j1", task="reveal due POLYROB ids (spends gas only)", schedule_spec="1m",
                   user_id="rob", next_run_at=None, payload=payload, max_duration_seconds=120)


@pytest.mark.asyncio
async def test_flag_off_refuses_and_calls_nothing(delivered, monkeypatch):
    monkeypatch.delenv(W.FLAG, raising=False)
    ok, why = await W.run_write_job(None, _job({"write_verb": SPEC}), tool_factory=_Tool)
    assert (ok, why) == (False, "write_jobs_disabled") and not _Tool.calls


@pytest.mark.asyncio
async def test_an_agent_authored_job_refuses(delivered):
    ok, why = await W.run_write_job(None, _job({"write_verb": SPEC, "authored_by": "agent"}),
                                    tool_factory=_Tool)
    assert (ok, why) == (False, "write_job_not_owner") and not _Tool.calls


@pytest.mark.asyncio
async def test_an_owner_job_runs_the_verb_with_no_turn_and_stays_quiet(delivered):
    ok, why = await W.run_write_job(None, _job({"write_verb": SPEC, "authored_by": "owner"}),
                                    tool_factory=_Tool)
    assert ok and why == "write_verb" and not delivered
    params, ctx = _Tool.calls[0]
    assert ctx is None and params.dry_run is False and params.max_ids == 10


@pytest.mark.asyncio
async def test_an_alert_is_delivered_and_says_what_the_job_spends(delivered):
    _Tool.text = "…\nALERT agent_nft_collection_reveal: 1 id(s) RE-COMMITTED [4]"
    ok, why = await W.run_write_job(None, _job({"write_verb": SPEC}), tool_factory=_Tool)
    assert ok and why == "write_verb_delivered"
    assert "RE-COMMITTED" in delivered[0][0] and "gas only" in delivered[0][0]


@pytest.mark.asyncio
async def test_a_verb_error_is_delivered(delivered):
    _Tool.error = "no collection is pinned on robinhood"
    ok, why = await W.run_write_job(None, _job({"write_verb": SPEC}), tool_factory=_Tool)
    assert not ok and delivered and "pinned" in delivered[0][0]


# -- the runner -------------------------------------------------------------------

@pytest.fixture
def runner(tmp_path, monkeypatch):
    from cron import runner as r
    for k in ("AUTONOMY_HALT", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    events = []
    monkeypatch.setattr(r, "_cron_ev", lambda job, outcome, reason=None, **kw:
                        events.append((outcome, reason, kw)))

    class Agent:                       # any session creation is a failure here
        def __getattr__(self, name):
            raise AssertionError(f"the agent was invoked: {name}")
    return r.make_agent_runner(task_agent=Agent(), data_dir=str(tmp_path)), events


@pytest.mark.asyncio
async def test_the_runner_refuses_a_write_job_while_the_flag_is_off(runner, monkeypatch):
    monkeypatch.delenv(W.FLAG, raising=False)
    run, events = runner
    assert await run(_job({"write_verb": SPEC})) is True
    assert events == [("skipped", "write_jobs_disabled", {"verb": "agent_nft.agent_nft_collection_reveal"})]


@pytest.mark.asyncio
async def test_the_runner_routes_a_write_job_without_the_agent(runner, monkeypatch):
    run, events = runner

    async def fake(task_agent, job, **kw):
        return True, "write_verb"
    monkeypatch.setattr(W, "run_write_job", fake)
    assert await run(_job({"write_verb": SPEC})) is True
    assert events == [("done", "write_verb", {"verb": "agent_nft.agent_nft_collection_reveal"})]


@pytest.mark.asyncio
async def test_the_owner_pause_skips_a_write_job(runner, tmp_path, monkeypatch):
    from core import autonomy_control as ac
    monkeypatch.setenv(W.FLAG, "true")
    called = []
    monkeypatch.setattr(W, "run_write_job", lambda *a, **k: called.append(1))
    run, events = runner
    ac.pause(str(tmp_path), scopes=("cron",), via="test")
    assert await run(_job({"write_verb": SPEC})) is True
    assert events[0][:2] == ("skipped", "paused") and not called


@pytest.mark.asyncio
async def test_a_pause_window_skips_a_write_job(runner, monkeypatch):
    import time
    monkeypatch.setenv(W.FLAG, "true")
    called = []
    monkeypatch.setattr(W, "run_write_job", lambda *a, **k: called.append(1))
    run, events = runner
    now = time.time()
    assert await run(_job({"write_verb": SPEC, "pause_windows": [[now - 60, now + 60]]})) is True
    assert events[0][:2] == ("skipped", "pause_window") and not called


# -- the service and the agent tool -------------------------------------------------

def _svc(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    return CronService(store, now=lambda: datetime.fromisoformat("2026-09-29T12:00:00")), store


def test_a_write_job_may_recur_every_minute_and_is_validated(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="reveal due POLYROB ids (gas only)", schedule_spec="*/1 * * * *",
                       user_id="rob", payload={"write_verb": SPEC})
    assert store.get(job.id).payload["write_verb"]["verb"] == "agent_nft.agent_nft_collection_reveal"
    with pytest.raises(ScheduleError, match="minimum"):
        svc.schedule(task="a paid agent run", schedule_spec="*/1 * * * *", user_id="rob")
    with pytest.raises(ScheduleError, match="not an allowlisted write verb"):
        svc.schedule(task="mint from cron", schedule_spec="1h", user_id="rob",
                     payload={"write_verb": {"verb": "agent_nft.agent_nft_collection_mint"}})
    with pytest.raises(ScheduleError, match="not both"):
        svc.schedule(task="both kinds at once", schedule_spec="1h", user_id="rob",
                     payload={"write_verb": SPEC, "read_verb": {
                         "verb": "defi_data.pool_metrics",
                         "params": {"token": "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"}}})  # gitleaks:allow (token address)


def test_dropping_the_write_verb_from_a_one_minute_job_refuses(tmp_path):
    svc, _store = _svc(tmp_path)
    job = svc.schedule(task="reveal due POLYROB ids (gas only)", schedule_spec="*/1 * * * *",
                       user_id="rob", payload={"write_verb": SPEC})
    with pytest.raises(ScheduleError, match="faster than"):
        svc.edit(job.id, user_id="rob", payload_updates={"write_verb": None})


@pytest.mark.asyncio
async def test_the_agent_tool_accepts_write_verb_only_on_an_owner_turn(tmp_path, monkeypatch):
    from tools import cronjob_tools as T
    from tools import goal_tools
    svc, store = _svc(tmp_path)
    tool = T.CronJobTool.__new__(T.CronJobTool)
    tool._cron_service = svc
    monkeypatch.setattr(T.CronJobTool, "_self_scheduling_refusal", staticmethod(lambda ctx: None))
    ctx = SimpleNamespace(user_id="rob")
    params = T.CronScheduleAction(task="reveal due POLYROB ids (gas only)",
                                  schedule="*/1 * * * *", write_verb=SPEC)

    monkeypatch.setattr(goal_tools, "owner_seat_turn", lambda c: False)
    res = await tool.cronjob_schedule(params, execution_context=ctx)
    assert res.error and "owner turn" in res.error and not store.list(user_id="rob")

    monkeypatch.setattr(goal_tools, "owner_seat_turn", lambda c: True)
    res = await tool.cronjob_schedule(params, execution_context=ctx)
    assert not res.error, res.error
    (job,) = store.list(user_id="rob")
    assert job.payload["authored_by"] == "owner" and job.payload["write_verb"]["verb"] == "agent_nft.agent_nft_collection_reveal"
