"""tools.defi.book — typed position rows for the Money Book (043 A34/A35 read side).

The book used to expose only a per-chain reconcile verdict + PROSE holdings.
`/money`'s Book tab needs typed rows (symbol/chain/amount/worth_now/entry/
since_entry). These tests pin the read side:
 - a chain with N positions yields N typed rows (symbol/chain/amount/worth_now
   present; entry/since_entry None WITH a reason until A35 writes the store);
 - a disagreement chain's rows still render, carrying the chain verdict;
 - an unreadable chain yields `unknown` rows with a reason, never dropped;
 - a value no store carries is None + a reason, never a fabricated `$0.00`.
"""
import pytest

from core.book import (
    NO_ENTRY_RECORDED_REASON, PNL_NEEDS_WORTH_REASON, BookRow,
)
from core.position_ledger import LedgerPosition
from tools.defi.book import book_rows

A1 = "0xb200000000000000000000Ea8625786a776539FB"
A2 = "0xb200000000000000000000b344cB4a1E8BD51968"
SOL = "So11111111111111111111111111111111111111112"


def _pos(symbol, address, qty):
    return LedgerPosition(symbol=symbol, address=address, qty=qty, line="")


class _AR:
    """Fake ActionResult — mirrors tests/unit/tools/defi/test_book_reader.py."""

    def __init__(self, content=None, error=None, metadata=None):
        self.extracted_content = content
        self.error = error
        self.metadata = metadata


def _enable_defi(monkeypatch):
    import tools.defi as defi_pkg
    monkeypatch.setattr(defi_pkg, "defi_data_enabled", lambda: True)


# --------------------------------------------------------------------------
# book_rows — the pure serializer
# --------------------------------------------------------------------------

def test_two_positions_on_a_chain_yield_two_typed_rows():
    chains_out = {
        "base": {
            "verdict": "clean",
            "report": {"matched": [f"PONS {A1} — 1 (ledger 1) ✓",
                                   f"WUF {A2} — 2 (ledger 2) ✓"]},
            "portfolio_text": (f"  {A1}  4,963,574.39 PONS  = $12.34\n"
                               f"  {A2}  3,997,365.84 WUF  = $1,000.50"),
            "error": None,
        },
    }
    positions = [_pos("PONS", A1, 4963574.39), _pos("WUF", A2, 3997365.84)]

    rows = book_rows(chains_out, positions)

    assert len(rows) == 2
    by_symbol = {r.symbol: r for r in rows}
    pons, wuf = by_symbol["PONS"], by_symbol["WUF"]

    # symbol / chain / amount / worth_now all present
    assert pons.chain == "base" and pons.state == "matched"
    assert pons.qty == 4963574.39
    assert pons.usd == 12.34           # parsed address-keyed from portfolio
    assert wuf.usd == 1000.50          # commas handled
    assert wuf.chain == "base"

    # entry / since_entry are None WITH a reason until A35's store lands
    for r in rows:
        assert r.entry is None and r.entry_reason == NO_ENTRY_RECORDED_REASON
        assert r.since_entry is None and r.since_entry_reason == NO_ENTRY_RECORDED_REASON


def test_disagreement_chain_rows_still_render_with_the_verdict():
    chains_out = {
        "base": {
            "verdict": "disagreement",
            "report": {"mismatched": [f"PONS {A1} — ledger 1 vs chain 2 (100% apart)"]},
            "portfolio_text": None,
            "error": None,
        },
    }
    rows = book_rows(chains_out, [_pos("PONS", A1, 1.0)])

    assert len(rows) == 1
    assert rows[0].state == "mismatched"
    assert rows[0].chain == "base"
    # no priced holding in this render -> None WITH a reason, never $0.00
    assert rows[0].usd is None and rows[0].worth_now_reason


def test_unreadable_chain_yields_unknown_rows_never_dropped():
    chains_out = {
        "base": {
            "verdict": "unverified",
            "report": None,
            "portfolio_text": None,
            "error": "node down",
        },
    }
    rows = book_rows(chains_out, [_pos("PONS", A1, 1.0)])

    assert len(rows) == 1                      # never dropped
    assert rows[0].state == "unknown"
    assert rows[0].chain == "base"
    assert rows[0].usd is None
    assert "node down" in rows[0].worth_now_reason


def test_confirmed_chain_wins_over_an_unreadable_one():
    chains_out = {
        "arbitrum": {"verdict": "unverified", "report": None,
                     "portfolio_text": None, "error": "rpc timeout"},
        "base": {"verdict": "clean",
                 "report": {"matched": [f"PONS {A1} — ok ✓"]},
                 "portfolio_text": f"  {A1}  1.0 PONS  = $5.00", "error": None},
    }
    rows = book_rows(chains_out, [_pos("PONS", A1, 1.0)])

    assert len(rows) == 1
    assert rows[0].state == "matched" and rows[0].chain == "base"
    assert rows[0].usd == 5.00


def test_ledger_row_no_readable_chain_backs_is_unbacked_never_dropped():
    chains_out = {
        "base": {"verdict": "clean", "report": {"matched": []},
                 "portfolio_text": "", "error": None},
    }
    rows = book_rows(chains_out, [_pos("GHOST", A1, 1.0)])

    assert len(rows) == 1                      # a stale ledger row is NOT dropped
    assert rows[0].state == "unbacked"
    assert rows[0].chain is None
    assert rows[0].usd is None and rows[0].worth_now_reason


def test_base58_position_only_considers_solana():
    chains_out = {
        "base": {"verdict": "clean", "report": {"matched": []},
                 "portfolio_text": "", "error": None},
        "solana": {"verdict": "clean",
                   "report": {"matched": [f"WSOL {SOL} — ok ✓"]},
                   "portfolio_text": f"  {SOL}  1.0 WSOL  = $200.00", "error": None},
    }
    rows = book_rows(chains_out, [_pos("WSOL", SOL, 1.0)])

    assert len(rows) == 1
    assert rows[0].chain == "solana" and rows[0].state == "matched"
    assert rows[0].usd == 200.00


def test_worth_from_excluded_price_line_is_none_with_reason():
    chains_out = {
        "base": {"verdict": "clean",
                 "report": {"matched": [f"PONS {A1} — ok ✓"]},
                 "portfolio_text": f"  {A1}  1.0 PONS  value EXCLUDED (low confidence price)",
                 "error": None},
    }
    rows = book_rows(chains_out, [_pos("PONS", A1, 1.0)])

    assert rows[0].usd is None
    assert rows[0].worth_now_reason  # a reason, not a blank/$0.00


def test_to_dict_shape_matches_the_money_book_row():
    row = BookRow(symbol="PONS", address=A1, chain="base", qty=1.0, usd=12.34,
                  state="matched", worth_now_reason=None,
                  entry=None, entry_reason=NO_ENTRY_RECORDED_REASON,
                  since_entry=None, since_entry_reason=NO_ENTRY_RECORDED_REASON)
    d = row.to_dict()
    assert set(d) >= {"symbol", "chain", "amount", "worth_now", "entry",
                      "since_entry"}
    assert d["amount"] == 1.0 and d["worth_now"] == 12.34
    assert d["entry"] is None and d["entry_reason"] == NO_ENTRY_RECORDED_REASON
    assert d["since_entry"] is None and d["since_entry_reason"] == NO_ENTRY_RECORDED_REASON


# --------------------------------------------------------------------------
# book_rows — entry/since_entry from the rail-written store (043 A35)
# --------------------------------------------------------------------------

def test_store_entry_computes_cost_basis_and_signed_pnl():
    from core.open_positions import PositionEntry
    chains_out = {
        "base": {"verdict": "clean",
                 "report": {"matched": [f"PONS {A1} — ok ✓"]},
                 "portfolio_text": f"  {A1}  1.0 PONS  = $12.34", "error": None},
    }
    entries = {A1.lower(): PositionEntry(chain="base", address=A1, symbol="PONS",
                                        qty=1.0, entry_usd=10.00, entry_ts=1.0)}
    rows = book_rows(chains_out, [_pos("PONS", A1, 1.0)], entries)
    assert rows[0].entry == 10.00                 # cost basis, a USD figure
    assert rows[0].entry_reason is None
    # since_entry is the SIGNED USD P&L: worth_now (12.34) - entry (10.00).
    assert rows[0].since_entry == pytest.approx(2.34)
    assert rows[0].since_entry_reason is None


def test_store_entry_but_no_worth_leaves_pnl_uncomputable():
    chains_out = {
        "base": {"verdict": "clean",
                 "report": {"matched": [f"PONS {A1} — ok ✓"]},
                 "portfolio_text": "", "error": None},        # no priced holding
    }
    from core.open_positions import PositionEntry
    entries = {A1.lower(): PositionEntry("base", A1, "PONS", 1.0, 10.0, 1.0)}
    rows = book_rows(chains_out, [_pos("PONS", A1, 1.0)], entries)
    assert rows[0].entry == 10.0                  # cost basis known
    assert rows[0].usd is None                    # worth unknown
    assert rows[0].since_entry is None
    assert rows[0].since_entry_reason == PNL_NEEDS_WORTH_REASON


def test_no_store_entry_reads_no_entry_recorded_not_the_old_placeholder():
    chains_out = {
        "base": {"verdict": "clean",
                 "report": {"matched": [f"PONS {A1} — ok ✓"]},
                 "portfolio_text": f"  {A1}  1.0 PONS  = $12.34", "error": None},
    }
    rows = book_rows(chains_out, [_pos("PONS", A1, 1.0)], {})
    assert rows[0].entry is None
    assert rows[0].entry_reason == NO_ENTRY_RECORDED_REASON
    assert rows[0].since_entry is None
    assert rows[0].since_entry_reason == NO_ENTRY_RECORDED_REASON


def test_negative_pnl_is_a_signed_loss():
    chains_out = {
        "base": {"verdict": "clean",
                 "report": {"matched": [f"PONS {A1} — ok ✓"]},
                 "portfolio_text": f"  {A1}  1.0 PONS  = $4.00", "error": None},
    }
    from core.open_positions import PositionEntry
    entries = {A1.lower(): PositionEntry("base", A1, "PONS", 1.0, 10.0, 1.0)}
    rows = book_rows(chains_out, [_pos("PONS", A1, 1.0)], entries)
    assert rows[0].since_entry == pytest.approx(-6.0)   # a loss, signed


# --------------------------------------------------------------------------
# read_book — the rows ride the /api/webgate/book response
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_book_carries_typed_rows(monkeypatch, tmp_path):
    _enable_defi(monkeypatch)

    async def fake_reconcile(self, params, execution_context=None):
        if params.chain == "base":
            return _AR(metadata={"report": {
                "matched": [f"PONS {A1} — ok ✓", f"WUF {A2} — ok ✓"],
                "verdict": "CLEAN"}})
        raise RuntimeError("node down")

    async def fake_portfolio(self, params, execution_context=None):
        if params.chain == "base":
            return _AR(content=f"holdings\n  {A1}  1.0 PONS  = $12.34\n"
                               f"  {A2}  2.0 WUF  = $8.00")
        return _AR(content="holdings text")

    from tools.defi.data_tool import DefiDataTool
    monkeypatch.setattr(DefiDataTool, "reconcile", fake_reconcile)
    monkeypatch.setattr(DefiDataTool, "portfolio", fake_portfolio)

    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "## Open positions\n\n"
        "| Token | Address | Size |\n"
        "|-------|---------|------|\n"
        f"| PONS | {A1} | 4,963,574.39 |\n"
        f"| WUF | {A2} | 3,997,365.84 |\n")

    from tools.defi.book import read_book
    body = await read_book("u1", str(tmp_path), chains=["base", "solana"],
                            ledger_path=str(ledger))

    rows = body["rows"]
    assert isinstance(rows, list) and len(rows) == 2
    for r in rows:
        assert r["chain"] == "base"
        assert r["worth_now"] is not None          # priced from portfolio
        assert r["entry"] is None and r["entry_reason"] == NO_ENTRY_RECORDED_REASON
        assert r["since_entry"] is None and r["since_entry_reason"] == NO_ENTRY_RECORDED_REASON


@pytest.mark.asyncio
async def test_read_book_rows_render_even_under_disagreement(monkeypatch, tmp_path):
    _enable_defi(monkeypatch)

    async def fake_reconcile(self, params, execution_context=None):
        return _AR(metadata={"report": {
            "mismatched": [f"PONS {A1} — ledger 1 vs chain 2"],
            "verdict": "DISAGREEMENT"}})

    async def fake_portfolio(self, params, execution_context=None):
        return _AR(content="holdings text")

    from tools.defi.data_tool import DefiDataTool
    monkeypatch.setattr(DefiDataTool, "reconcile", fake_reconcile)
    monkeypatch.setattr(DefiDataTool, "portfolio", fake_portfolio)

    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "## Open positions\n\n"
        "| Token | Address | Size |\n"
        "|-------|---------|------|\n"
        f"| PONS | {A1} | 1 |\n")

    from tools.defi.book import read_book
    body = await read_book("u1", str(tmp_path), chains=["base"],
                            ledger_path=str(ledger))

    assert body["verdict"] == "disagreement"
    assert len(body["rows"]) == 1                  # rows still render
    assert body["rows"][0]["state"] == "mismatched"


@pytest.mark.asyncio
async def test_read_book_disabled_flag_returns_empty_rows(monkeypatch, tmp_path):
    import tools.defi as defi_pkg
    monkeypatch.setattr(defi_pkg, "defi_data_enabled", lambda: False)

    from tools.defi.book import read_book
    body = await read_book("u1", str(tmp_path))

    assert body["rows"] == []
    assert "DEFI_DATA_ENABLED is off" in body["error"]


@pytest.mark.asyncio
async def test_read_book_joins_the_rail_written_store(monkeypatch, tmp_path):
    """End-to-end (043 A35): a store row written for THIS tenant makes the book's
    entry (cost basis) + since_entry (signed P&L) real, and it is tenant-scoped."""
    _enable_defi(monkeypatch)

    async def fake_reconcile(self, params, execution_context=None):
        return _AR(metadata={"report": {
            "matched": [f"PONS {A1} — ok ✓"], "verdict": "CLEAN"}})

    async def fake_portfolio(self, params, execution_context=None):
        return _AR(content=f"holdings\n  {A1}  1.0 PONS  = $12.34")

    from tools.defi.data_tool import DefiDataTool
    monkeypatch.setattr(DefiDataTool, "reconcile", fake_reconcile)
    monkeypatch.setattr(DefiDataTool, "portfolio", fake_portfolio)

    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "## Open positions\n\n"
        "| Token | Address | Size |\n"
        "|-------|---------|------|\n"
        f"| PONS | {A1} | 1.0 |\n")

    # A recorded buy wrote this row for owner "u1" at the data-dir store.
    from core.open_positions import PositionDelta, apply_delta
    store = str(tmp_path / "open_positions.db")
    apply_delta("u1", PositionDelta("base", A1, "PONS", 1.0, 10.00),
                db_path=store, now=1.0)

    from tools.defi.book import read_book
    body = await read_book("u1", str(tmp_path), chains=["base"],
                            ledger_path=str(ledger))
    row = body["rows"][0]
    assert row["entry"] == 10.00
    assert row["since_entry"] == pytest.approx(2.34)   # 12.34 worth - 10.00 cost

    # A different tenant reading the same book sees no entry (tenant-scoped).
    other = await read_book("intruder", str(tmp_path), chains=["base"],
                             ledger_path=str(ledger))
    assert other["rows"][0]["entry"] is None
    assert other["rows"][0]["entry_reason"] == NO_ENTRY_RECORDED_REASON
