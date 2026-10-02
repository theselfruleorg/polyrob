"""A fully-exited row still sitting in the table is not an open position.

⚠️ The defect, measured in production on 2026-09-23: the 08:05→08:09 EXIT took
PAIR to a full exit and wrote the record the DELTA way — it edited the `Size`
cell to ``0 — FULL EXIT 2026-09-23 08:08 UTC (…)`` and left the row in
``## Open positions`` instead of moving it to ``## Closed positions``. The row
parses, its size parses as ``0``, and the slot-cap preflight counts rows. So at
08:50 the SCOUT rail skipped with ``open_rows: 6`` against a cap of 6 while the
book actually held **5** — a real exit freed real capital and the entry rail
could not see it.

The fix is deliberately NOT in the parser. ``parse_open_positions`` reports what
the table SAYS, and `reconcile` needs that: a zero row matched against a zero
chain balance is a match, and dropping it would invent a "held on chain, not in
ledger" discrepancy. What changes is that the two places which COUNT open
positions — the slot-cap preflight and the owner-facing status line — now ask
which rows are open rather than how many rows exist.

**UNKNOWN is never closed.** A row whose size will not parse stays open: the
ledger's own contract is that an unreadable figure is never read as zero, and
here the fail-safe direction is to keep occupying the slot rather than free one
the agent may still be holding.
"""
import pytest

from core.position_ledger import (LedgerPosition, open_positions,
                                  parse_open_positions)

ADDR_A = "0x" + "a" * 40
ADDR_B = "0x" + "b" * 40


def _row(qty):
    return LedgerPosition(symbol="TKN", address=ADDR_A, qty=qty, line="|…|")


# --- the discriminator --------------------------------------------------------- #

def test_a_held_size_is_open():
    assert _row(487.06660919).is_open is True


def test_a_zero_size_is_closed():
    assert _row(0.0).is_open is False


def test_an_unparseable_size_stays_open():
    """UNKNOWN is never zero — and never a freed slot."""
    assert _row(None).is_open is True


def test_a_negative_size_is_closed_not_open():
    """No honest ledger writes one, but a stray '-0' must not occupy a slot."""
    assert _row(-0.0).is_open is False
    assert _row(-1.0).is_open is False


def test_open_positions_filters_and_preserves_order():
    rows = [_row(1.0), _row(0.0), _row(None), _row(2.0)]
    assert [r.qty for r in open_positions(rows)] == [1.0, None, 2.0]


def test_open_positions_tolerates_an_empty_book():
    assert open_positions([]) == []


# --- the production table, in miniature ---------------------------------------- #

PROD_SHAPED = f"""
## Open positions

| Token | Address | Size | Entry price | Thesis | Date |
|-------|---------|------|-------------|--------|------|
| JUGGERNAUT | {ADDR_A} | 332.627699 | $0.00848 | held | 2026-09-14 |
| PAIR | {ADDR_B} | 0 — FULL EXIT 2026-09-23 08:08 UTC (487.06660919 → 0.00103313 WETH ≈ $2.83) | ~$0.003946 | exited | 2026-09-16 |

## Closed positions
"""


def test_the_parser_still_reports_every_row_the_table_holds():
    """`reconcile` compares the ledger against chain row by row — it must keep
    seeing the exited row, or a lingering dust balance reads as unrecorded."""
    rows, err = parse_open_positions(PROD_SHAPED)
    assert err is None
    assert [r.symbol for r in rows] == ["JUGGERNAUT", "PAIR"]
    assert rows[1].qty == pytest.approx(0.0)


def test_the_count_that_gates_a_new_entry_sees_one_open_position():
    """The 08:50 SCOUT skip in miniature: two rows, one free slot."""
    rows, _ = parse_open_positions(PROD_SHAPED)
    assert len(rows) == 2
    assert len(open_positions(rows)) == 1


# --- the seats that count ------------------------------------------------------ #

def test_the_slot_cap_preflight_counts_open_rows_not_table_rows(tmp_path, monkeypatch):
    from cron import preflight

    ledger = tmp_path / "kb-root-position-ledger.md"
    ledger.write_text(PROD_SHAPED, encoding="utf-8")
    monkeypatch.setenv("POSITION_LEDGER_PATH", str(ledger))

    job = type("J", (), {"payload": {"preflight": {"kind": "slot_cap",
                                                   "max_open_rows": 2}}})()
    # Two rows, cap 2 — the old count skipped here. One is fully exited.
    assert preflight.preflight_skip(job, data_dir=str(tmp_path)) is None


def test_the_slot_cap_still_skips_when_the_book_is_genuinely_full(tmp_path, monkeypatch):
    from cron import preflight

    full = PROD_SHAPED.replace("| 0 — FULL EXIT 2026-09-23 08:08 UTC (487.06660919 → 0.00103313 WETH ≈ $2.83) |",
                               "| 487.06660919 |")
    ledger = tmp_path / "kb-root-position-ledger.md"
    ledger.write_text(full, encoding="utf-8")
    monkeypatch.setenv("POSITION_LEDGER_PATH", str(ledger))

    job = type("J", (), {"payload": {"preflight": {"kind": "slot_cap",
                                                   "max_open_rows": 2}}})()
    skip = preflight.preflight_skip(job, data_dir=str(tmp_path))
    assert skip is not None and skip.reason == "no_slot"
    assert skip.attrs["open_rows"] == 2


def test_an_unreadable_ledger_still_runs_the_tick(tmp_path, monkeypatch):
    """Unchanged: the preflight fails OPEN. Filtering must not become a gate."""
    from cron import preflight

    monkeypatch.setenv("POSITION_LEDGER_PATH", str(tmp_path / "absent.md"))
    job = type("J", (), {"payload": {"preflight": {"kind": "slot_cap",
                                                   "max_open_rows": 1}}})()
    assert preflight.preflight_skip(job, data_dir=str(tmp_path)) is None


def test_the_status_line_counts_open_positions(tmp_path, monkeypatch):
    from core.status_snapshot import _positions_line

    ledger = tmp_path / "kb-root-position-ledger.md"
    ledger.write_text(PROD_SHAPED, encoding="utf-8")
    monkeypatch.setenv("POSITION_LEDGER_PATH", str(ledger))

    line = _positions_line(str(tmp_path))
    assert "1 recorded" in line
    assert "JUGGERNAUT" in line and "PAIR" not in line


def test_a_book_of_only_exited_rows_reads_as_none_not_unknown(tmp_path, monkeypatch):
    """Distinct from an unreadable ledger, which must keep saying UNKNOWN."""
    from core.status_snapshot import _positions_line

    only_exited = PROD_SHAPED.replace("| 332.627699 |", "| 0 — FULL EXIT |")
    ledger = tmp_path / "kb-root-position-ledger.md"
    ledger.write_text(only_exited, encoding="utf-8")
    monkeypatch.setenv("POSITION_LEDGER_PATH", str(ledger))

    line = _positions_line(str(tmp_path))
    assert "none recorded" in line
    assert "UNKNOWN" not in line
