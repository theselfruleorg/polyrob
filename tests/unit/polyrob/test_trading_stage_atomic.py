"""098 W1 MO-18: the stage and the start of its window are ONE row write (no
silent second-table note), and the pull pass keeps the hunt job in step with
the mandate (the mandate is the source)."""
import time

import pytest

from core import trading_mandate as tm
from polyrob.app import trading_jobs as tj
from tests.unit.polyrob.test_086_w4_trading_lifecycle import U, World, _loss  # noqa: F401
import tests.trading_fixture  # noqa: F401


@pytest.fixture(autouse=True)
def _pins(monkeypatch):
    from core import cron_lanes
    from polyrob.app import trading_lifecycle as tl
    monkeypatch.setattr(cron_lanes, "missing_pinned_skills", lambda payload, user_id: [])
    tl._PRICED.clear()


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_failed_demotion_note_cannot_repromote(w, monkeypatch):
    from polyrob.app import trading_lifecycle as tl
    m = w.mandate(chains=["base"])
    tm.set_stage(m.id, "small", path=w.mp)
    monkeypatch.setattr(tl, "_set_note", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("database is locked")))
    w.realized = [_loss(-60.0, ago_s=600)]
    res = w.run()
    assert res.promoted == [f"{m.id}:small->probe"]
    m = tm.get(m.id, path=w.mp)
    assert m.demoted_at and m.stage == "probe"
    tm.release(m.id, actor=tm.OWNER_SEAT, path=w.mp)
    assert not w.run().promoted          # the old probe close predates demoted_at


def test_small_window_starts_at_promotion_not_creation(w):
    m = w.mandate(chains=["base"], now=time.time() - 7200)
    before = time.time()
    m = tm.set_stage(m.id, "small", path=w.mp)
    assert m.stage_since >= before
    assert tm.STAGE_MACHINE.validate() == []


def test_stage_cas_loses_to_a_concurrent_move(w):
    m = w.mandate(chains=["base"])
    tm.set_stage(m.id, "small", path=w.mp)
    with pytest.raises(tm.MandateChanged):
        tm.set_stage(m.id, "normal", path=w.mp, from_stage="probe")
    assert tm.get(m.id, path=w.mp).stage == "small"


def test_stopped_mandate_takes_no_stage_or_cooldown(w):
    m = w.mandate(chains=["base"])
    tm.set_status(m.id, tm.STOPPED, actor=tm.OWNER_SEAT, path=w.mp)
    with pytest.raises(tm.MandateChanged):
        tm.set_stage(m.id, "small", path=w.mp)
    assert tm.set_cooldown(m.id, 1.0, path=w.mp).cooldown_until is None


def test_pull_pass_resumes_hunt_for_active_mandate(w):
    m = w.mandate()
    tj.create_mandate_jobs(w.svc, m, via="telegram")
    m = tm.set_status(m.id, tm.PAUSED, actor=tm.OWNER_SEAT, path=w.mp)
    tj.set_hunt(w.svc, m, paused=True, via="telegram")
    # /trade resume: the mandate moved, the hunt resume failed
    tm.set_status(m.id, tm.ACTIVE, actor=tm.OWNER_SEAT, path=w.mp)
    assert {j.status for j in w.jobs(tj.HUNT)} == {"paused"}
    w.run()
    assert {j.status for j in w.jobs(tj.HUNT)} == {"scheduled"}


def test_pull_pass_keeps_an_owner_pause_of_the_hunt_job(w):
    m = w.mandate()
    tj.create_mandate_jobs(w.svc, m, via="telegram")
    for j in w.jobs(tj.HUNT):
        w.svc.pause(j.id, user_id=U, via="telegram")   # the owner paused the job itself
    w.run()
    assert {j.status for j in w.jobs(tj.HUNT)} == {"paused"}


def test_pull_pass_pauses_hunt_for_paused_mandate(w):
    m = w.mandate()
    tj.create_mandate_jobs(w.svc, m, via="telegram")
    tm.set_status(m.id, tm.PAUSED, actor=tm.OWNER_SEAT, path=w.mp)   # hunt pause failed
    w.run()
    assert {j.status for j in w.jobs(tj.HUNT)} == {"paused"}
