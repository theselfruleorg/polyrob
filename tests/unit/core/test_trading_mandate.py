"""086 W2: the trading mandate store + the promotion ladder."""
import os
import types

import pytest

from core import trading_mandate as tm
from core.trading_mandate import (MandateError, MandateUnreadable, OWNER_SEAT, create, get,
                                  get_active, latch, next_stage, release, set_stage,
                                  set_status, stage_ticket)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "trading.db")


def _mk(db, **kw):
    args = dict(actor=OWNER_SEAT, budget_usd=150.0, chains=["robinhood", "Solana"], path=db,
                now=1000.0)
    args.update(kw)
    return create("u1", **args)


def test_create_applies_the_preset_defaults(db):
    m = _mk(db)
    assert m.status == "active" and m.stage == "probe" and m.post_public is False
    assert m.chains == ("robinhood", "solana")
    assert (m.preset, m.ticket_usd, m.slots, m.exit_preset) == ("memecoin", 3.0, 8,
                                                                "memecoin_default")
    assert m.report == tm.DEFAULT_REPORT
    again = get(m.id, path=db)
    assert again == m


def test_only_the_owner_seat_creates(db):
    with pytest.raises(MandateError, match="owner seat"):
        _mk(db, actor="agent")
    assert not os.path.exists(db)


@pytest.mark.parametrize("bad", [
    dict(budget_usd=0), dict(budget_usd="x"), dict(chains=[]), dict(preset="yolo"),
    dict(ticket_usd=500.0), dict(breakers={"stop_pct": 3.0}), dict(breakers={"slots": 2}),
    dict(exit_preset="nope"),
])
def test_create_refuses_bad_fields(db, bad):
    with pytest.raises(MandateError):
        _mk(db, **bad)


def test_owner_may_tune_breakers_at_create(db):
    m = _mk(db, breakers={"daily_loss_pct": 15.0})
    assert m.effective_preset().breakers.daily_loss_pct == 15.0
    assert m.effective_preset().slots == 8


def test_majors_ticket_from_allocation(db):
    m = _mk(db, preset="majors", chains=["base"])
    assert m.ticket_usd == 15.0 and m.slots == 3


def test_one_live_mandate_per_user(db):
    m = _mk(db)
    with pytest.raises(MandateError, match="already has a live"):
        _mk(db)
    set_status(m.id, "stopped", actor=OWNER_SEAT, path=db)
    m2 = _mk(db)
    assert get_active("u1", path=db).id == m2.id
    assert create("u2", actor=OWNER_SEAT, budget_usd=10, chains=["base"], path=db)


def test_reads_never_create_and_absent_is_none(db):
    assert get("m_x", path=db) is None
    assert get_active("u1", path=db) is None
    assert tm.entries_since("m_x", 0, path=db) == 0
    assert not os.path.exists(db)


def test_unreadable_is_not_empty(tmp_path):
    bad = tmp_path / "trading.db"
    bad.write_bytes(b"not a sqlite file at all" * 100)
    with pytest.raises(MandateUnreadable):
        get_active("u1", path=str(bad))
    with pytest.raises(MandateUnreadable):
        get("m", path=str(bad))


def test_status_rules(db):
    m = _mk(db)
    assert set_status(m.id, "paused", actor="lifecycle", path=db).status == "paused"
    with pytest.raises(MandateError, match="only the owner"):
        set_status(m.id, "active", actor="agent", path=db)
    assert set_status(m.id, "active", actor=OWNER_SEAT, path=db).status == "active"
    with pytest.raises(MandateError):
        set_status(m.id, "halted", actor=OWNER_SEAT, path=db)
    with pytest.raises(MandateError, match="only the owner"):
        set_status(m.id, "stopped", actor="agent", path=db)
    s = set_status(m.id, "stopped", actor=OWNER_SEAT, path=db)
    assert s.status == "stopped" and s.stopped_by == OWNER_SEAT
    with pytest.raises(MandateError, match="final"):
        set_status(m.id, "active", actor=OWNER_SEAT, path=db)


def test_latch_persists_and_only_the_owner_releases(db):
    m = _mk(db)
    h = latch(m.id, "breaker:daily_loss", path=db, now=2000.0)
    assert (h.status, h.halted_reason, h.halted_at) == ("halted", "breaker:daily_loss", 2000.0)
    # the first reason stands
    assert latch(m.id, "breaker:drawdown", path=db).halted_reason == "breaker:daily_loss"
    # a re-read (a restart) still sees it
    assert get(m.id, path=db).status == "halted"
    with pytest.raises(MandateError):
        set_status(m.id, "active", actor=OWNER_SEAT, path=db)
    # a pause would leave the halt without release() — refused; stop stays open
    with pytest.raises(MandateError, match="release"):
        set_status(m.id, "paused", actor=OWNER_SEAT, path=db)
    with pytest.raises(MandateError, match="only the owner"):
        release(m.id, actor="agent", path=db)
    r = release(m.id, actor=OWNER_SEAT, path=db, now=3000.0)
    assert r.status == "active" and r.halted_reason is None
    assert r.released_at == 3000.0 and get(m.id, path=db).released_at == 3000.0
    with pytest.raises(MandateError, match="not halted"):
        release(m.id, actor=OWNER_SEAT, path=db)


def test_apply_breaker_cooldown_vs_latch(db):
    m = _mk(db)
    c = tm.apply_breaker(m.id, "consecutive_losses", path=db, now=5000.0)
    assert c.status == "active" and c.cooldown_until == 5000.0 + 6 * 3600
    h = tm.apply_breaker(m.id, "drawdown", path=db)
    assert h.status == "halted" and h.halted_reason == "breaker:drawdown"


def test_record_entry_counts(db):
    m = _mk(db)
    tm.record_entry(m.id, chain="solana", token="Mint1", usd=3.0, path=db, now=100.0)
    tm.record_entry(m.id, chain="solana", token="Mint2", usd=3.0, path=db, now=200.0)
    assert tm.entries_since(m.id, 150.0, path=db) == 1


def test_set_stage(db):
    m = _mk(db)
    assert set_stage(m.id, "small", path=db).stage == "small"
    with pytest.raises(MandateError):
        set_stage(m.id, "huge", path=db)


def test_mandate_id_from_context():
    assert tm.mandate_id_from_context(types.SimpleNamespace(metadata={"mandate": " m_1 "})) == "m_1"
    assert tm.mandate_id_from_context(types.SimpleNamespace(metadata={})) is None
    assert tm.mandate_id_from_context(None) is None


# -- promotion ladder ---------------------------------------------------------------

def _m(**kw):
    d = dict(id="m", user_id="u", status="active", budget_usd=150.0,
             chains=("robinhood", "solana"), preset="memecoin", ticket_usd=3.0, slots=8,
             actor=OWNER_SEAT, created_at=0.0, updated_at=0.0, stage="probe")
    d.update(kw)
    return tm.Mandate(**d)


def test_stage_ticket():
    assert stage_ticket(_m(stage="probe")) == 1.0
    assert stage_ticket(_m(stage="small")) == 1.5
    assert stage_ticket(_m(stage="normal")) == 3.0
    assert stage_ticket(_m(stage="probe", ticket_usd=0.5)) == 0.5


def test_probe_to_small_needs_every_chain():
    assert next_stage(_m(), probe_passed_chains=["solana"]) == "probe"
    assert next_stage(_m(), probe_passed_chains=["Solana", "robinhood"]) == "small"


def test_small_to_normal_needs_trades_and_pnl():
    m = _m(stage="small")
    risk = tm.normal_added_risk_usd(m)
    assert risk == 6.0  # (3 - 1.5) x 8 slots x 50 % stop
    assert next_stage(m, small_realized_usd=10.0, small_closed_trades=4) == "small"
    assert next_stage(m, small_realized_usd=5.99, small_closed_trades=9) == "small"
    assert next_stage(m, small_realized_usd=6.0, small_closed_trades=5) == "normal"


def test_a_breaker_drops_one_stage():
    assert next_stage(_m(stage="normal"), breaker_tripped=True) == "small"
    assert next_stage(_m(stage="small"), breaker_tripped=True) == "probe"
    assert next_stage(_m(stage="probe"), breaker_tripped=True) == "probe"
