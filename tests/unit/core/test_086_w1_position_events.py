"""086 W1 §3.3: the book fires ONE position event after each committed change,
to many listeners; a listener that raises never breaks the book write."""
import pytest

from core import open_positions as op
from core import position_events as pe
from core.open_positions import PositionDelta, apply_delta
from core.position_retire import retire_row

MEME = "0xb200000000000000000000ea8625786a776539fb"
ACCT = "0x00000000000000000000000000000000000000a1"


@pytest.fixture
def events():
    got = []
    pe.register_listener(got.append)
    yield got
    pe.unregister_listener(got.append)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "open_positions.db")


def _kinds(events):
    return [e.kind for e in events]


def test_registry_is_many_listeners_and_a_raising_one_is_skipped(events):
    second = []

    def boom(_e):
        raise RuntimeError("listener bug")

    pe.register_listener(boom)
    pe.register_listener(second.append)
    pe.register_listener(second.append)  # idempotent
    try:
        pe.emit(pe.OPENED, user_id="u1", chain="base", account="", address=MEME, qty=1)
    finally:
        pe.unregister_listener(boom)
        pe.unregister_listener(second.append)
    assert _kinds(events) == ["opened"] and len(second) == 1
    assert second[0].address == MEME and second[0].ts > 0


def test_a_buy_opens_a_second_buy_and_a_partial_change_a_full_sell_closes(events, db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10.0, 3.0), db_path=db, now=1.0)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10.0, 3.0), db_path=db, now=2.0)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -5.0, proceeds_usd=2.0),
                db_path=db, now=3.0)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -15.0, proceeds_usd=9.0,
                                    close_reason="take_profit"), db_path=db, now=4.0)
    assert _kinds(events) == ["opened", "changed", "changed", "closed"]
    opened, grown, partial, closed = events
    assert opened.qty == 10.0 and opened.usd == 3.0 and opened.ts == 1.0
    assert grown.qty == 20.0
    assert partial.qty == 15.0 and partial.usd == 2.0
    assert closed.qty == 0.0 and closed.usd == 9.0 and closed.reason == "take_profit"
    assert closed.symbol == "MEME" and closed.user_id == "u1"


def test_a_sell_of_nothing_and_a_failed_write_fire_nothing(events, db, monkeypatch):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -1.0, proceeds_usd=1.0), db_path=db)
    assert events == []
    import core.sqlite_util as su

    def broken(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(su, "execute_retry", broken)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 1.0, 1.0), db_path=db)
    assert events == []


def test_a_listener_error_never_breaks_the_book_write(db):
    def boom(_e):
        raise RuntimeError("listener bug")

    pe.register_listener(boom)
    try:
        apply_delta("u1", PositionDelta("base", MEME, "MEME", 10.0, 3.0), db_path=db)
    finally:
        pe.unregister_listener(boom)
    assert op.get_position("u1", "base", MEME, db_path=db).qty == 10.0


def test_status_changes_fire_their_kind(events, db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10.0, 3.0), db_path=db)
    events.clear()
    assert op.set_status("u1", "base", MEME, op.STATUS_QUARANTINED, reason="look-alike",
                         db_path=db)
    assert op.set_status("u1", "base", MEME, op.STATUS_QUARANTINED, db_path=db) is False
    assert op.set_status("u1", "base", MEME, op.STATUS_DUST, reason="dust", db_path=db)
    assert op.set_status("u1", "base", MEME, op.STATUS_WRITTEN_OFF, reason="owner",
                         db_path=db)
    assert _kinds(events) == ["changed", "dust", "written_off"]
    assert events[0].reason == "look-alike" and events[2].qty == 10.0


def test_a_buy_into_a_dust_row_reopens_it(events, db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10.0, 3.0), db_path=db)
    op.set_status("u1", "base", MEME, op.STATUS_DUST, db_path=db)
    events.clear()
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10.0, 3.0), db_path=db)
    assert _kinds(events) == ["opened"]


def test_a_buy_after_a_retire_opens_a_fresh_row(events, db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 0.16, 3.0), db_path=db)
    out = retire_row("u1", "base", MEME, account="", measured_qty=0.0, measured_usd=0.0,
                     proof="balanceOf read: 0 raw", db_path=db, now=50.0)
    assert out.retired
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 5.0, 1.0), db_path=db)
    assert _kinds(events) == ["opened", "retired", "opened"]
    assert events[1].reason == "retired" and events[1].ts == 50.0


def test_seed_inherited_opens_with_reason_inherited(events, db):
    assert op.seed_inherited("u1", chain="base", account=ACCT, address=MEME, symbol="MEME",
                             qty=4.0, db_path=db)
    assert op.seed_inherited("u1", chain="base", account=ACCT, address=MEME, symbol="MEME",
                             qty=4.0, db_path=db) is False
    assert _kinds(events) == ["opened"]
    assert events[0].reason == "inherited" and events[0].account == ACCT
    assert events[0].usd is None


def test_telemetry_rows_for_open_and_close(monkeypatch, db):
    rows = []
    import core.event_log as el
    monkeypatch.setattr(el, "emit", lambda kind, **kw: rows.append((kind, kw)))
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10.0, 3.0), db_path=db)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -4.0, proceeds_usd=1.0), db_path=db)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -6.0, proceeds_usd=2.0,
                                    close_reason="stop_loss"), db_path=db)
    kinds = [k for k, _ in rows]
    assert kinds == ["position_opened", "position_closed"]
    attrs = rows[1][1]["attrs"]
    assert attrs["reason"] == "stop_loss" and attrs["kind"] == "closed"
    assert attrs["chain"] == "base" and attrs["address"] == MEME
    assert rows[1][1]["user_id"] == "u1"


def test_telemetry_listener_is_fail_open(monkeypatch):
    import core.event_log as el

    def broken(*a, **k):
        raise RuntimeError("log down")

    monkeypatch.setattr(el, "emit", broken)
    pe.emit(pe.OPENED, user_id="u1", chain="base", account="", address=MEME)  # no raise
