"""086 W4: the trading mandate's lifecycle — arm / disarm on book events, the pull
pass (UNPROTECTED, ORPHAN, dust, breakers, promotion, stop), idempotent and fail-safe.

Fakes: a real CronService on a tmp ``cron.db``, a real tmp ``trading.db``, and the
book read through monkeypatched ``read_mandate_book`` / ``realized_for`` /
``set_status`` (the kernel's selection is ``core.money.mandate_gate``'s own test).
"""
import time

import pytest

from core import cron_lanes
from core import position_events as pe
from core import trading_mandate as tm
from core.open_positions import RealizedEntry
from cron.jobs import CronJobStore
from cron.service import CronService
from polyrob.app import trading_jobs as tj
from polyrob.app import trading_lifecycle as tl

U = "owner-1"
TOK = "0x" + "ab" * 20


@pytest.fixture(autouse=True)
def _pins_resolve(monkeypatch):
    monkeypatch.setattr(cron_lanes, "missing_pinned_skills", lambda payload, user_id: [])
    tl._PRICED.clear()


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.mp = str(tmp_path / "trading.db")
        self.bp = str(tmp_path / "open_positions.db")
        self.svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
        self.rows = []
        self.realized = []
        self.statuses = []
        self.book_ok = True
        import core.money.mandate_gate as mg
        import core.open_positions as op

        def _read(m, *, user_id, chain="", token=None, book_path=None):
            if not self.book_ok:
                raise RuntimeError("book unreadable")
            return list(self.rows), None

        def _set_status(user_id, chain, address, status, **kw):
            self.statuses.append((chain, address, status))
            self.rows = [r for r in self.rows if r["address"] != address]
            return True

        monkeypatch.setattr(mg, "read_mandate_book", _read)
        monkeypatch.setattr(op, "realized_for", lambda uid, **kw: list(self.realized))
        monkeypatch.setattr(op, "set_status", _set_status)

    def mandate(self, **kw):
        kw.setdefault("budget_usd", 150.0)
        kw.setdefault("chains", ["solana", "base"])
        kw.setdefault("now", time.time() - 3600)
        return tm.create(U, actor=tm.OWNER_SEAT, path=self.mp, **kw)

    def hold(self, address=TOK, chain="base", qty=10.0, cost=3.0):
        self.rows.append({"chain": chain, "account": "", "address": address, "qty": qty,
                          "entry_usd": cost, "entry_ts": time.time(), "status": "open",
                          "origin": "trade"})

    def event(self, kind, address=TOK, chain="base", reason=""):
        return pe.PositionEvent(kind=kind, user_id=U, chain=chain, account="",
                                address=address, symbol="MEME", qty=10.0, usd=3.0,
                                reason=reason, ts=time.time())

    def jobs(self, role=None):
        return [j for j in tj.mandate_jobs(self.svc)
                if tj.holding(j) and (role is None or tj.role(j) == role)]

    def run(self, **kw):
        kw.setdefault("price_fn", None)
        return tl.pull_pass(svc=self.svc, mandate_path=self.mp, book_path=self.bp,
                            memory_db=str(self.mp + ".nomem"), **kw)


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _on(w, ev):
    return tl.on_event(ev, svc=w.svc, mandate_path=w.mp, book_path=w.bp)


def test_opened_arms_the_exit_job_with_the_owner_grant_and_books_the_entry(w):
    m = w.mandate()
    w.hold()
    assert _on(w, w.event(pe.OPENED)) == "armed"
    (job,) = w.jobs(tj.EXIT)
    p = job.payload
    assert p["mandate"] == m.id and p["actor"] == "owner_seat"
    assert p["position"] == {"chain": "base", "account": "", "address": TOK.lower()}
    assert p["rig"] == "money_rail" and p["skills"] == tj.EXIT_SKILLS
    assert p["precheck"]["verb"] == "defi_data.position_mark"
    assert p["precheck"]["cancel_when"] == ["closed", "dust"]
    assert job.schedule_spec == "15m"
    assert tm.entries_since(m.id, 0, path=w.mp) == 1
    # Idempotent: a second event arms nothing.
    assert _on(w, w.event(pe.OPENED)) == ""
    assert len(w.jobs(tj.EXIT)) == 1


def test_the_money_rail_keeps_its_tools_on_the_cron_read(w):
    from tools.cronjob_tools import resolve_cron_tools
    w.mandate()
    w.hold()
    _on(w, w.event(pe.OPENED))
    (job,) = w.jobs(tj.EXIT)
    assert "defi_trade" in resolve_cron_tools(job.payload)


def test_opened_ignores_what_is_not_the_mandates(w):
    w.mandate()
    assert _on(w, w.event(pe.OPENED)) == ""            # not in the kernel's selection
    w.hold(chain="robinhood")
    assert _on(w, w.event(pe.OPENED, chain="robinhood")) == ""   # not a mandate chain
    w.hold()
    assert _on(w, w.event(pe.OPENED, reason="inherited")) == ""
    assert not w.jobs(tj.EXIT)


def test_no_mandate_no_store_does_nothing(w):
    assert _on(w, w.event(pe.OPENED)) == ""
    res = w.run()
    assert not res.changed() and not res.notices


@pytest.mark.parametrize("kind", list(pe.TERMINAL_KINDS))
def test_a_terminal_event_cancels_the_positions_exit_job(w, kind):
    w.mandate()
    w.hold()
    _on(w, w.event(pe.OPENED))
    assert _on(w, w.event(kind)) == "cancelled"
    assert not w.jobs(tj.EXIT)
    (job,) = [j for j in tj.mandate_jobs(w.svc) if tj.role(j) == tj.EXIT]
    assert job.payload["stopped"]["via"] == "mandate"


def test_pull_pass_arms_an_unprotected_position_and_tells_the_owner_once(w):
    m = w.mandate()
    w.hold()
    res = w.run()
    assert len(res.armed) == 1 and len(res.notices) == 1
    assert "UNPROTECTED" in res.notices[0].text
    # The agent cancels it (cronjob_cancel does not refuse): re-armed, said once.
    (job,) = w.jobs(tj.EXIT)
    w.svc.cancel(job.id, user_id=U, via="agent")
    res = w.run()
    assert len(res.armed) == 1 and not res.notices
    res = w.run()
    assert not res.armed
    assert m.id


def test_pull_pass_cancels_an_orphan_exit_job(w):
    w.mandate()
    w.hold()
    w.run()
    w.rows.clear()                    # the position closed without an event
    res = w.run()
    assert len(res.orphaned) == 1 and not w.jobs(tj.EXIT)


def test_an_unreadable_book_does_nothing_destructive(w):
    w.mandate()
    w.hold()
    w.run()
    w.book_ok = False
    res = w.run()
    assert not res.orphaned and w.jobs(tj.EXIT)
    assert any(s.endswith(":book") for s in res.skipped)


def test_an_unreadable_mandate_store_does_nothing(w, tmp_path):
    w.mp = str(tmp_path)              # a directory: exists, cannot be read as a db
    res = w.run()
    assert res.skipped == ["mandates"] and not res.changed()


def test_dust_is_stored_and_its_job_retired(w):
    w.mandate()
    w.hold(qty=10.0, cost=3.0)
    w.run()
    res = w.run(price_fn=lambda chain, addr: 0.01)       # worth $0.10 of $3
    assert res.dust and w.statuses == [("base", TOK, "dust")]
    assert not w.jobs(tj.EXIT)


def test_a_probe_ticket_a_little_down_is_not_dust(w):
    w.mandate()
    w.hold(qty=1.0, cost=1.0)
    res = w.run(price_fn=lambda chain, addr: 0.9)
    assert not res.dust and w.jobs(tj.EXIT)


def _loss(usd, ago_s=60, chain="base", addr=TOK):
    return RealizedEntry(chain=chain, address=addr, symbol="X", account="",
                         realized_usd=usd, known_legs=1, unknown_legs=0, sold_qty=1.0,
                         updated_ts=time.time() - ago_s)


def test_a_breaker_latches_pauses_the_hunt_keeps_exits_and_cards_once(w):
    m = w.mandate()
    tj.create_mandate_jobs(w.svc, m, via="telegram")
    w.hold()
    w.run()
    w.realized = [_loss(-16.0)]       # > 10 % of $150 in a day
    res = w.run()
    assert res.breakers == [f"{m.id}:daily_loss"]
    assert tm.get(m.id, path=w.mp).status == tm.HALTED
    (hunt,) = [j for j in tj.mandate_jobs(w.svc) if tj.role(j) == tj.HUNT]
    assert hunt.status == "paused"
    assert w.jobs(tj.EXIT) and w.jobs(tj.REVIEW)
    (n,) = res.notices
    assert n.card is not None and n.card.line == f"/trade release {m.id}"
    assert not w.run().notices        # once


def test_consecutive_losses_cool_down_without_a_latch(w):
    m = w.mandate(chains=["base"])
    w.realized = [_loss(-0.5, 400 - i, addr="0x%040d" % i) for i in range(4)]
    res = w.run()
    assert res.breakers == [f"{m.id}:consecutive_losses"]
    got = tm.get(m.id, path=w.mp)
    assert got.status == tm.ACTIVE and got.cooldown_until > time.time()
    assert len(res.notices) == 1 and res.notices[0].card is None


def test_the_probe_promotes_to_small_once_every_chain_closed_a_trip(w):
    m = w.mandate(chains=["base"])
    w.realized = [_loss(0.1)]
    res = w.run()
    assert res.promoted == [f"{m.id}:probe->small"]
    assert tm.get(m.id, path=w.mp).stage == "small"
    assert "small" in res.notices[0].text
    assert not w.run().promoted


def test_a_released_breaker_does_not_retrip_and_its_demotion_sticks(w):
    m = w.mandate(chains=["base"])
    tm.set_stage(m.id, "small", path=w.mp)
    w.realized = [_loss(-60.0, ago_s=600)]
    res = w.run()
    assert res.breakers and res.promoted == [f"{m.id}:small->probe"]
    assert tm.get(m.id, path=w.mp).status == tm.HALTED
    tm.release(m.id, actor=tm.OWNER_SEAT, path=w.mp)
    res = w.run()                     # the same history: no new trip, no re-promotion
    assert not res.breakers and not res.promoted
    m = tm.get(m.id, path=w.mp)
    assert (m.status, m.stage) == (tm.ACTIVE, "probe")


def test_a_stopped_mandate_cancels_hunt_and_review_but_keeps_exits(w):
    m = w.mandate()
    tj.create_mandate_jobs(w.svc, m, via="telegram")
    w.hold()
    w.run()
    tm.set_status(m.id, tm.STOPPED, actor=tm.OWNER_SEAT, path=w.mp)
    res = w.run()
    assert len(res.stopped) == 2
    assert not w.jobs(tj.HUNT) and not w.jobs(tj.REVIEW) and w.jobs(tj.EXIT)
    w.rows.clear()                    # the position closes: the exit job goes too
    assert w.run().orphaned and not w.jobs(tj.EXIT)


def test_an_owner_lane_stop_is_not_overridden(w):
    from surfaces.telegram.job_ops import jobs_reply
    m = w.mandate()
    w.hold()
    w.run()
    (job,) = w.jobs(tj.EXIT)
    data_dir = str(w.svc.store.db_path).rsplit("/", 1)[0]
    jobs_reply(U, data_dir, ["stop", job.id[:8], "--forever"])
    res = w.run()
    assert not res.armed and "cannot arm" in res.notices[0].text
    assert m.id in res.notices[0].text and not w.run().notices


def test_an_unconfirmed_mandate_gets_no_exit_job(w):
    from dataclasses import replace
    m = replace(w.mandate(), actor="agent")
    with pytest.raises(ValueError):
        tj.arm_exit(w.svc, m, chain="base", account="", address=TOK)


def test_the_listener_never_raises(w, monkeypatch):
    monkeypatch.setattr(tm, "get_active", lambda *a, **k: (_ for _ in ()).throw(
        tm.MandateUnreadable("x")))
    assert _on(w, w.event(pe.OPENED)) == ""


def test_boot_wiring_registers_the_listener_and_the_loop(monkeypatch):
    added = {}

    class H:
        def _add(self, name, ticker):
            added[name] = ticker

    tl.start(H(), object(), "/tmp/x")
    try:
        assert tl._listener in pe._LISTENERS
        assert added["trading"].interval_seconds == tl.INTERVAL_SEC
    finally:
        tl.stop()
        tl._STATE["data_dir"] = None
    assert tl._listener not in pe._LISTENERS


def test_live_mandates_lists_every_non_stopped_mandate(w):
    assert tm.live_mandates(path=w.mp) == []         # absent store: none, not created
    m = w.mandate()
    other = tm.create("owner-2", actor=tm.OWNER_SEAT, budget_usd=10, chains=["base"],
                      path=w.mp)
    assert {x.id for x in tm.live_mandates(path=w.mp)} == {m.id, other.id}
    tm.set_status(other.id, tm.STOPPED, actor=tm.OWNER_SEAT, path=w.mp)
    assert [x.id for x in tm.live_mandates(path=w.mp)] == [m.id]


def test_draft_validates_like_create_and_writes_nothing(w):
    d = tm.draft(U, budget_usd=150, chains=["Solana"], preset="memecoin")
    assert d.actor == "" and d.chains == ("solana",) and d.ticket_usd == 3.0
    with pytest.raises(tm.MandateError):
        tm.draft(U, budget_usd=0, chains=["base"])
    assert tm.live_mandates(path=w.mp) == []


def _job(w, job_id):
    return w.svc.store.get(job_id, user_id=U)


def test_the_hunt_states_the_stage_ticket_and_rungs_and_follows_a_promotion(w):
    m = w.mandate(chains=["base"])
    hunt, review = tj.create_mandate_jobs(w.svc, m, via="telegram")
    assert "Stage: probe — one entry costs at most $1.00 now" in hunt.task
    assert "Tier rungs: A at most $1.00, B at most $1.00, C at most $1.00." in hunt.task
    assert review.payload["skills"] == ["trading-mandate"]
    w.realized = [_loss(0.1)]
    w.run()
    task = _job(w, hunt.id).task
    assert "Stage: small — one entry costs at most $1.50 now" in task
    assert "A at most $1.50, B at most $1.50, C at most $1.00" in task


def test_the_exit_task_carries_the_stored_plan(w, monkeypatch):
    import core.position_plans as pp
    w.mandate()
    w.hold()
    _on(w, w.event(pe.OPENED))
    (job,) = w.jobs(tj.EXIT)
    assert tj.PLAN_NONE in job.task and "exits" in job.payload["skills"]

    class _Plan:
        def render(self):
            return "plan (memecoin, confidence 0.60): fresh pool — invalidated if: dev sells."

    monkeypatch.setattr(pp, "plan_for", lambda *a, **k: _Plan())
    w.run()
    task = _job(w, job.id).task
    assert "Plan: plan (memecoin, confidence 0.60): fresh pool" in task
    assert tj.PLAN_NONE not in task
