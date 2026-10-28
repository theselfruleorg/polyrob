"""086 W5: ``/trade`` is the trading mandate — start (one intake card), status,
pause, resume, stop (+ the close-all card), release.

It REPLACED the one-goal ``/trade <task>``: the owner says what to trade once,
confirms ONE card, and code creates every job (the 10-12 record: four
improvised goals, two blocked ten days on asks, one book-wide exit cron that
ran 59 times on dust).
"""
import time

import pytest

from core import cron_lanes
from core import trading_mandate as tm
from core.surfaces import cards
from core.surfaces.command_reply import reply_text
from cron.jobs import CronJobStore
from cron.service import CronService
from polyrob.app import trading_jobs as tj
from surfaces.telegram import trade_ops

U = "owner-1"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_lanes, "missing_pinned_skills", lambda payload, user_id: [])
    cards.set_store(cards.CardStore(str(tmp_path / "cards.db")))
    import core.money.mandate_gate as mg
    monkeypatch.setattr(mg, "read_mandate_book", lambda m, **kw: ([], None))
    yield
    cards.set_store(None)


@pytest.fixture
def seat(tmp_path):
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    mp = str(tmp_path / "trading.db")

    def run(*words):
        return trade_ops.trade_reply(U, str(tmp_path), list(words), svc=svc, mandate_path=mp,
                                     price_fn=lambda c, a: None)

    run.svc, run.mp = svc, mp
    return run


def _confirm(seat, reply):
    """Tap Confirm: the card's ONE line, run on the seat as if typed."""
    card = cards.store().get(reply.card_id)
    p = cards.press(card.card_id, "ok", U)
    assert p.run == card.line and p.run.startswith("/trade start d")
    out = seat(*p.run.split()[1:])
    cards.finish(card.card_id, out)
    return out


def test_bare_start_writes_nothing_and_names_the_presets(seat):
    out = seat("start")
    assert "memecoin" in out and "majors" in out and "allocation" in out
    assert tm.get_active(U, path=seat.mp) is None
    assert not tj.mandate_jobs(seat.svc)


def test_start_with_fields_is_one_card_with_the_preset_numbers(seat):
    reply = seat("start", "150", "robinhood,solana", "memecoin")
    text = reply_text(reply)
    assert reply.card_id
    for want in ("$150 allocation", "$3 tickets", "up to 8 open", "-50% stop",
                 "+100% sell 50%", "48 h time stop", "-10% of the allocation",
                 "about $1.50", "Confirm:"):
        assert want in text, want
    assert tm.get_active(U, path=seat.mp) is None          # nothing before Confirm
    assert not tj.mandate_jobs(seat.svc)


def test_confirm_creates_the_mandate_and_its_jobs_in_one_line(seat):
    reply = seat("start", "budget=150", "chains=solana", "ticket=2", "slots=4")
    line = cards.store().get(reply.card_id).line
    out = _confirm(seat, reply)
    m = tm.get_active(U, path=seat.mp)
    assert m is not None and m.actor == tm.OWNER_SEAT and m.status == tm.ACTIVE
    assert (m.budget_usd, m.chains, m.ticket_usd, m.slots) == (150.0, ("solana",), 2.0, 4)
    assert f"Trading mandate {m.id} started" in out
    jobs = tj.mandate_jobs(seat.svc, mandate_id=m.id)
    assert sorted(tj.role(j) for j in jobs) == ["hunt", "review"]
    hunt = next(j for j in jobs if tj.role(j) == "hunt")
    assert hunt.payload["actor"] == "owner_seat" and hunt.payload["rig"] == "money_rail"
    assert hunt.payload["skills"][:2] == ["trading-mandate", "treasury-trading"]
    assert hunt.payload["precheck"]["verb"] == "defi_data.discovery_count"
    assert hunt.schedule_spec == "30m"
    review = next(j for j in jobs if tj.role(j) == "review")
    assert review.payload["rig"] == "research" and "precheck" not in review.payload
    # The line runs once: again -> refused (live mandate), after a stop -> used.
    assert "one live mandate" in seat(*line.split()[1:])
    seat("stop")
    assert "already used" in seat(*line.split()[1:])


def test_a_bad_field_is_refused_before_any_card(seat):
    assert "❌" in seat("start", "0", "solana")
    assert "❌" in seat("start", "150", "solana", "preset=yolo")
    assert "❌" in seat("start", "150", "solana", "colour=red")


def test_one_live_mandate(seat):
    _confirm(seat, seat("start", "150", "solana"))
    assert "one live mandate" in reply_text(seat("start", "50", "base"))


def test_job_failure_leaves_no_mandate(seat, monkeypatch):
    reply = seat("start", "150", "solana")

    def boom(*a, **k):
        raise RuntimeError("cron store full")

    monkeypatch.setattr(tj, "create_mandate_jobs", boom)
    out = _confirm(seat, reply)
    assert "Nothing was started" in out and tm.get_active(U, path=seat.mp) is None


def test_pause_resume_keep_exits_and_move_the_hunt(seat):
    _confirm(seat, seat("start", "150", "solana"))
    m = tm.get_active(U, path=seat.mp)
    assert "PAUSED" in seat("pause")
    assert tm.get(m.id, path=seat.mp).status == tm.PAUSED
    hunt = next(j for j in tj.mandate_jobs(seat.svc) if tj.role(j) == "hunt")
    assert seat.svc.store.get(hunt.id, user_id=U).status == "paused"
    assert "ACTIVE" in seat("resume")
    assert seat.svc.store.get(hunt.id, user_id=U).status == "scheduled"


def test_release_only_a_halted_mandate(seat):
    _confirm(seat, seat("start", "150", "solana"))
    m = tm.get_active(U, path=seat.mp)
    assert "not halted" in seat("release")
    tm.latch(m.id, "breaker:daily_loss", path=seat.mp)
    assert "/trade release" in seat("resume")
    assert "ACTIVE" in seat("release")
    assert tm.get(m.id, path=seat.mp).status == tm.ACTIVE


def test_stop_ends_hunt_and_review_and_offers_close_all(seat, monkeypatch):
    import core.money.mandate_gate as mg
    _confirm(seat, seat("start", "150", "solana"))
    m = tm.get_active(U, path=seat.mp)
    monkeypatch.setattr(mg, "read_mandate_book",
                        lambda mm, **kw: ([{"chain": "solana", "address": "Mint1"}], None))
    reply = seat("stop")
    assert tm.get(m.id, path=seat.mp).status == tm.STOPPED
    assert not [j for j in tj.mandate_jobs(seat.svc) if tj.holding(j)]
    card = cards.store().get(reply.card_id)
    assert card.line == f"/trade stop {m.id} close-all"
    out = seat(*card.line.split()[1:])
    assert "Close all" in out
    (close,) = [j for j in tj.mandate_jobs(seat.svc) if tj.holding(j)]
    assert tj.role(close) == "close_all" and close.one_shot
    assert close.payload["actor"] == "owner_seat"


def test_stop_with_nothing_open_needs_no_card(seat):
    _confirm(seat, seat("start", "150", "solana"))
    out = seat("stop")
    assert isinstance(out, str) and "nothing is left running" in out


def test_status_is_the_one_renderer(seat):
    assert "no mandate" in seat("status")
    _confirm(seat, seat("start", "150", "solana"))
    assert "Trading:" in seat("status") and "ACTIVE" in seat("status")


def test_only_the_owner(tmp_path):
    assert "Only the owner" in trade_ops.trade_reply(None, str(tmp_path), ["status"])


def test_an_unreadable_store_changes_nothing(tmp_path):
    out = trade_ops.trade_reply(U, str(tmp_path), ["pause"], mandate_path=str(tmp_path))
    assert "could not be read" in out


def test_usage():
    assert "start" in trade_ops.trade_reply(U, "/tmp", []) and "release" in trade_ops.USAGE


def test_the_card_expires_with_its_draft(seat):
    from core import trading_intake as ti
    ref, _ = ti.save_draft(U, {"budget_usd": 10, "chains": ["base"]}, origin="owner",
                           path=seat.mp, now=time.time() - ti.DRAFT_TTL_S - 5)
    assert "expired" in seat("start", ref)
    assert "no mandate draft" in trade_ops.trade_reply("someone-else", "/tmp",
                                                       ["start", ref], mandate_path=seat.mp)
