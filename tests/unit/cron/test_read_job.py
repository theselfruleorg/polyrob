"""W9 (core handoff; 090 §3/§7, D45): the deterministic read job and the
per-job pause window."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from cron import read_job as R
from cron.jobs import CronJob, CronJobStore
from cron.schedule import ScheduleError
from cron.service import CronService

PNL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
SPEC = {"verb": "defi_data.pool_metrics", "params": {"token": PNL}}


# -- pause windows -----------------------------------------------------------

def test_windows_accept_epoch_iso_a_bare_pair_and_objects():
    assert R.normalize_pause_windows([100, 200]) == [[100.0, 200.0]]
    iso = R.normalize_pause_windows([["2026-10-01T12:00:00Z", "2026-10-02T12:00:00+00:00"]])
    assert iso == [[datetime.fromisoformat("2026-10-01T12:00:00+00:00").timestamp(),
                    datetime.fromisoformat("2026-10-02T12:00:00+00:00").timestamp()]]
    assert R.normalize_pause_windows([{"from": 1, "to": 2}]) == [[1.0, 2.0]]
    assert R.normalize_pause_windows(None) is None


@pytest.mark.parametrize("bad", [[[200, 100]], [["soon", 5]], [[1, 2, 3]], "x",
                                 [[True, 5]], [[i, i + 1] for i in range(9)]])
def test_malformed_windows_refuse(bad):
    with pytest.raises(R.ReadJobError):
        R.normalize_pause_windows(bad)


def test_active_window_is_half_open():
    p = {"pause_windows": [[100, 200], [300, 400]]}
    assert R.active_pause_window(p, 99) is None
    assert R.active_pause_window(p, 100) == [100.0, 200.0]
    assert R.active_pause_window(p, 200) is None
    assert R.active_pause_window(p, 350) == [300.0, 400.0]
    assert R.active_pause_window({}, 150) is None


def test_a_stored_window_that_no_longer_parses_holds_the_job():
    assert R.active_pause_window({"pause_windows": [["x", "y"]]}, 1) is not None


# -- read verbs ---------------------------------------------------------------

def test_only_allowlisted_reads_on_non_money_tools():
    from agents.task.agent.core.correspondent_gate import is_high_impact
    from core.tool_capabilities import ids_with
    for verb, (_m, _c, action, _p) in R.READ_VERBS.items():
        tool = verb.split(".")[0]
        assert tool not in set(ids_with("money")), verb
        assert tool not in set(ids_with("high_impact")), verb
        assert not is_high_impact(f"{tool}_{action}"), verb


def test_validate_refuses_unknown_verbs_bad_params_and_bad_delivery():
    with pytest.raises(R.ReadJobError, match="not a deterministic read verb"):
        R.validate_read_verb({"verb": "defi_trade.swap", "params": {}})
    with pytest.raises(R.ReadJobError, match="do not validate"):
        R.validate_read_verb({"verb": "defi_data.pool_metrics", "params": {}})
    with pytest.raises(R.ReadJobError, match="deliver_on"):
        R.validate_read_verb(dict(SPEC, deliver_on="sometimes"))
    assert R.validate_read_verb(SPEC)["deliver_on"] == "alert"


def test_should_deliver():
    assert R.should_deliver("alert", True, "ALERT: x\nrest")
    assert not R.should_deliver("alert", True, "all good")
    assert R.should_deliver("alert", False, "error")
    assert R.should_deliver("always", True, "fine")
    assert not R.should_deliver("never", False, "error")


class _Tool:
    text, error, calls = "pool_metrics ok", None, []

    async def pool_metrics(self, params):
        _Tool.calls.append(params)
        return SimpleNamespace(extracted_content=self.text, error=self.error)


@pytest.fixture
def delivered(monkeypatch):
    sent = []

    async def deliver(task_agent, job, text, *, target, deliver_target=None):
        sent.append((text, target))
        return True
    monkeypatch.setattr("cron.delivery.deliver_result", deliver)
    _Tool.calls, _Tool.text, _Tool.error = [], "pool_metrics ok", None
    return sent


def _job(payload):
    return CronJob(id="j1", task="monitor the PNL pool", schedule_spec="30m", user_id="rob",
                   next_run_at=None, payload=payload, max_duration_seconds=180)


@pytest.mark.asyncio
async def test_read_job_runs_the_verb_and_stays_quiet_without_an_alert(delivered):
    ok, why = await R.run_read_job(None, _job({"read_verb": SPEC}), tool_factory=_Tool)
    assert ok and why == "read_verb" and not delivered
    assert _Tool.calls[0].token == PNL


@pytest.mark.asyncio
async def test_an_alert_is_delivered(delivered):
    _Tool.text = "ALERT: code hash check failed\npool_metrics …"
    ok, why = await R.run_read_job(None, _job({"read_verb": SPEC, "deliver": "email"}),
                                   tool_factory=_Tool)
    assert ok and why == "read_verb_delivered"
    assert delivered == [(_Tool.text, "email")]


@pytest.mark.asyncio
async def test_a_verb_error_is_an_alert(delivered):
    _Tool.error = "the position read could not be completed"
    ok, why = await R.run_read_job(None, _job({"read_verb": SPEC}), tool_factory=_Tool)
    assert not ok and delivered and delivered[0][0] == _Tool.error


# -- the runner ---------------------------------------------------------------

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
async def test_a_tick_inside_its_pause_window_is_a_zero_skip(runner):
    import time
    run, events = runner
    now = time.time()
    assert await run(_job({"pause_windows": [[now - 60, now + 60]]})) is True
    assert events[0][:2] == ("skipped", "pause_window")


@pytest.mark.asyncio
async def test_the_runner_routes_a_read_job_without_the_agent(runner, monkeypatch):
    run, events = runner

    async def fake(task_agent, job, **kw):
        return True, "read_verb"
    monkeypatch.setattr(R, "run_read_job", fake)
    assert await run(_job({"read_verb": SPEC})) is True
    assert events == [("done", "read_verb", {"verb": "defi_data.pool_metrics"})]


@pytest.mark.asyncio
async def test_the_owner_pause_holds_a_read_job(runner, tmp_path):
    from core import autonomy_control as ac
    run, events = runner
    ac.pause(str(tmp_path), scopes=("cron",), via="test")
    assert await run(_job({"read_verb": SPEC})) is True
    assert events[0][:2] == ("skipped", "paused")


# -- the service ----------------------------------------------------------------

def _svc(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    return CronService(store, now=lambda: datetime.fromisoformat("2026-09-29T12:00:00")), store


def test_schedule_normalizes_and_refuses(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="monitor the pool", schedule_spec="30m", user_id="rob",
                       payload={"read_verb": SPEC, "pause_windows": [100, 200]})
    stored = store.get(job.id)
    assert stored.payload["pause_windows"] == [[100.0, 200.0]]
    assert stored.payload["read_verb"]["deliver_on"] == "alert"
    with pytest.raises(ScheduleError, match="end after"):
        svc.schedule(task="buyback rail", schedule_spec="6h", user_id="rob",
                     payload={"pause_windows": [[5, 1]]})
    with pytest.raises(ScheduleError, match="not a deterministic read verb"):
        svc.schedule(task="buyback rail", schedule_spec="6h", user_id="rob",
                     payload={"read_verb": {"verb": "defi_trade.swap"}})


def test_edit_sets_and_clears_a_window(tmp_path):
    svc, store = _svc(tmp_path)
    job = svc.schedule(task="buyback rail", schedule_spec="6h", user_id="rob")
    assert svc.edit(job.id, user_id="rob",
                    payload_updates={"pause_windows": ["2026-10-01T11:00:00Z",
                                                       "2026-10-03T12:00:00Z"]}) == ["pause_windows"]
    assert len(store.get(job.id).payload["pause_windows"]) == 1
    with pytest.raises(ScheduleError):
        svc.edit(job.id, user_id="rob", payload_updates={"pause_windows": [[3, 2]]})
    assert svc.edit(job.id, user_id="rob", payload_updates={"pause_windows": None}) == ["pause_windows"]
    assert "pause_windows" not in store.get(job.id).payload


# -- 071 §3.9: public watches with an owner-set threshold ---------------------

def test_the_public_defi_reads_are_allowlisted_and_own_wallet_reads_are_not():
    for verb in ("defi_data.price", "defi_data.token_info", "defi_data.wallet_holdings"):
        assert verb in R.READ_VERBS
    for verb in ("defi_data.portfolio", "defi_data.positions", "defi_data.reconcile"):
        assert verb not in R.READ_VERBS     # no owner turn on a cron tick


def test_alert_spec_validates():
    spec = R.validate_read_verb({"verb": "defi_data.price",
                                 "params": {"chain": "base", "address": PNL},
                                 "alert": {"field": "usd", "below": "0.5"}})
    assert spec["alert"] == {"field": "usd", "below": 0.5}
    for bad in ({"below": 1}, {"field": "usd"}, {"field": "usd", "above": "x"}, "usd"):
        with pytest.raises(R.ReadJobError):
            R.normalize_alert(bad)


def test_alert_lines_fire_on_bounds_and_on_unknown():
    a = {"field": "quote.usd", "below": 1.0, "above": 5.0}
    assert R.alert_lines(a, {"quote": {"usd": 3.0}}) == []
    assert "below" in R.alert_lines(a, {"quote": {"usd": 0.5}})[0]
    assert "above" in R.alert_lines(a, {"quote": {"usd": 9}})[0]
    assert "UNKNOWN" in R.alert_lines(a, {})[0]
    assert "UNKNOWN" in R.alert_lines(a, None)[0]
    assert R.alert_lines(None, {}) == []


@pytest.mark.asyncio
async def test_a_threshold_hit_makes_the_job_deliver():
    class _P:
        async def price(self, params, *a):
            return SimpleNamespace(error=None, extracted_content="price text",
                                   metadata={"usd": 0.2})
    spec = {"verb": "defi_data.price", "params": {"chain": "base", "address": PNL},
            "alert": {"field": "usd", "below": 0.5}}
    ok, text = await R.call_read_verb(spec, tool_factory=_P)
    assert ok and text.startswith("ALERT: usd")
    assert R.should_deliver("alert", ok, text)
