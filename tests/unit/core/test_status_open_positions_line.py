"""The /status open-positions line reads the rail book (open_positions.db).

**UNKNOWN is never zero**: an unreadable book is not an empty one.
"""


def test_status_money_line_reads_the_rail_book(tmp_path):
    """The status line reads open_positions.db (the ONE store), not the ledger."""
    from core.open_positions import PositionDelta, apply_delta, open_positions_db_path
    from core.status_snapshot import _money_section
    db = open_positions_db_path(str(tmp_path))
    apply_delta("u1", PositionDelta(chain="base", address="0x" + "a" * 40,
                                    symbol="PONSGIRL", qty=10.0, cost_usd=5.0), db_path=db)
    sec = _money_section("u1", {"runtime": {}, "treasury": {}}, str(tmp_path))
    body = "\n".join(sec.lines)
    assert "open positions: 1 open in the rail book (PONSGIRL)" in body
    assert "reconcile" in body  # the remedy, not just the fact


def test_status_money_line_says_none_without_a_rail_book(tmp_path):
    from core.status_snapshot import _money_section
    sec = _money_section("u1", {"runtime": {}, "treasury": {}}, str(tmp_path))
    assert "open positions: none in the rail book" in "\n".join(sec.lines)


def test_status_money_line_says_unknown_on_an_unreadable_book(tmp_path):
    from core.status_snapshot import _money_section
    (tmp_path / "open_positions.db").write_text("not a database")
    sec = _money_section("u1", {"runtime": {}, "treasury": {}}, str(tmp_path))
    body = "\n".join(sec.lines)
    assert "open positions: UNKNOWN" in body and "not the same as none" in body


def test_status_and_trading_count_one_selection_with_quarantine_named(tmp_path):
    """116 W6.2: a quarantined look-alike is counted apart, the same way on both lines."""
    from core.open_positions import (STATUS_QUARANTINED, PositionDelta, apply_delta,
                                     open_positions_db_path, set_status)
    from core.status_snapshot import _money_section
    from core.trading_report import trading_view
    db = open_positions_db_path(str(tmp_path))
    for i, sym in enumerate(("AAA", "BBB", "PNL")):
        apply_delta("u1", PositionDelta(chain="base", address="0x" + str(i) * 40,
                                        symbol=sym, qty=1.0, cost_usd=1.0), db_path=db)
    set_status("u1", "base", "0x" + "2" * 40, STATUS_QUARANTINED, reason="look-alike",
               db_path=db)
    body = "\n".join(_money_section("u1", {"runtime": {}, "treasury": {}}, str(tmp_path)).lines)
    assert "open positions: 2 open (+1 quarantined) in the rail book (AAA, BBB)" in body
    lines = trading_view("u1", now=1.0, data_dir=str(tmp_path))["lines"]
    assert any("open positions: 2 open (+1 quarantined) in the book, no mandate — AAA, BBB"
               in ln for ln in lines), lines
