"""The position-ledger reader (core tier) + the /status open-positions line.

D7 of the 2026-08-28 status work: `treasury cash flow … net $+0.00 (open
positions NOT included)` cannot distinguish an empty book from two unsellable
bags. It was two. The governing rule is the one `tools/defi/reconcile` already
states: **UNKNOWN is never zero.**
"""
import os

import pytest

from core.position_ledger import (parse_open_positions, read_open_positions,
                                  resolve_position_ledger_path)

LEDGER = """# Treasury

## Open positions

| Token | Address | Size | Entry price | Thesis | Date |
|-------|---------|------|-------------|--------|------|
| PONSGIRL | 0xb200000000000000000000Ea8625786a776539FB | 4,963,574.39 | ~$0.00000040 | base launch | 2026-08-26 |
| WUF | 0xb200000000000000000000b344cB4a1E8BD51968 | 3,997,365.84 | ~$0.00000050 | base launch | 2026-08-26 |

## Closed positions

| Token | Address | ... |
| OLD | 0xb200000000000000000000b344cB4a1E8BD51900 | x |
"""

FLAT = """## Open positions

| Token | Address | Size |
|-------|---------|------|
| (none — book flat) | | |

## Closed positions
"""


def test_parses_only_the_open_table(tmp_path):
    rows, err = parse_open_positions(LEDGER)
    assert err is None
    assert [r.symbol for r in rows] == ["PONSGIRL", "WUF"]
    assert rows[0].qty == pytest.approx(4963574.39)


def test_flat_book_is_empty_not_an_error():
    rows, err = parse_open_positions(FLAT)
    assert err is None and rows == []


def test_missing_section_is_an_error_not_an_empty_book():
    rows, err = parse_open_positions("# Treasury\n\nno table here\n")
    assert rows == []
    assert err is not None and "Open positions" in err


def test_explicit_env_path_wins(tmp_path, monkeypatch):
    p = tmp_path / "my-ledger.md"
    p.write_text(LEDGER)
    monkeypatch.setenv("POSITION_LEDGER_PATH", str(p))
    assert resolve_position_ledger_path(str(tmp_path)) == str(p)
    rows, err = read_open_positions(str(tmp_path))
    assert err is None and len(rows) == 2


def test_default_project_filename_is_found(tmp_path, monkeypatch):
    monkeypatch.delenv("POSITION_LEDGER_PATH", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / "kb-root-position-ledger.md").write_text(LEDGER)
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(project))
    rows, err = read_open_positions(str(tmp_path))
    assert err is None and len(rows) == 2


def test_missing_ledger_is_unknown_never_zero(tmp_path, monkeypatch):
    monkeypatch.delenv("POSITION_LEDGER_PATH", raising=False)
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(tmp_path / "nope"))
    rows, err = read_open_positions(str(tmp_path))
    assert rows == []
    assert err is not None  # the caller must render UNKNOWN, not "none"


def test_ambiguous_ledgers_refuse_to_guess(tmp_path, monkeypatch):
    monkeypatch.delenv("POSITION_LEDGER_PATH", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / "a-position-ledger.md").write_text(LEDGER)
    (project / "b-position-ledger.md").write_text(FLAT)
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(project))
    assert resolve_position_ledger_path(str(tmp_path)) is None


def test_tools_reconcile_reexports_the_same_parser():
    """One parser, one tier down — not a second copy (the discovery-decoder rule)."""
    from tools.defi import reconcile as rec
    assert rec.parse_open_positions is parse_open_positions


def test_status_money_line_names_the_open_positions(tmp_path, monkeypatch):
    from core.status_snapshot import _money_section
    monkeypatch.delenv("POSITION_LEDGER_PATH", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / "kb-root-position-ledger.md").write_text(LEDGER)
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(project))

    sec = _money_section("u1", {"runtime": {}, "treasury": {}}, str(tmp_path))
    body = "\n".join(sec.lines)
    assert "open positions: 2 recorded" in body
    assert "PONSGIRL" in body and "WUF" in body
    assert "reconcile" in body  # the remedy, not just the fact


def test_status_money_line_says_unknown_when_the_ledger_is_gone(tmp_path, monkeypatch):
    from core.status_snapshot import _money_section
    monkeypatch.delenv("POSITION_LEDGER_PATH", raising=False)
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(tmp_path / "nope"))

    sec = _money_section("u1", {"runtime": {}, "treasury": {}}, str(tmp_path))
    body = "\n".join(sec.lines)
    assert "open positions: UNKNOWN" in body
    assert "not the same as none" in body


def test_status_money_line_says_none_on_a_genuinely_flat_book(tmp_path, monkeypatch):
    from core.status_snapshot import _money_section
    monkeypatch.delenv("POSITION_LEDGER_PATH", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / "kb-root-position-ledger.md").write_text(FLAT)
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(project))

    sec = _money_section("u1", {"runtime": {}, "treasury": {}}, str(tmp_path))
    assert "open positions: none recorded" in "\n".join(sec.lines)
