"""`defi_data.positions` lists ledger-open holdings that have no rail-store row.

Holdings bought before `open_positions.db` existed (prod: JUGGERNAUT, HOOKR) are
open in the position LEDGER only. `positions` used to omit them, so the exits
skill gave them no stop check. Pins:
 - store-only: unchanged, no ledger section;
 - ledger-only: listed, basis unknown, no P&L, no high-water, NO store write;
 - both: a ledger row that the store tracks (on any chain) is not doubled;
 - unreadable ledger: "could not read", never a silent empty.
"""
import os

import pytest

from core import open_positions
from core.open_positions import PositionDelta, apply_delta
from tools.defi.data_tool import DefiDataTool, PositionsParams

MEME = "0xb200000000000000000000ea8625786a776539fb"
JUGG = "0xb20000000000000000000000000000000000a001"
HOOK = "0xb20000000000000000000000000000000000b002"


def _px(price, conf="high"):
    return type("P", (), {"price_usd": price, "confidence": conf})()


def _ctx(uid="owner-1"):
    return type("Ctx", (), {"user_id": uid, "session_id": "s"})()


def _ledger(*rows):
    body = "".join(f"| {s} | {a} | {q} | 12.40 |\n" for s, a, q in rows)
    return ("## Open positions\n\n| Token | Address | Qty | Entry USD |\n"
            "|---|---|---|---|\n" + body + "\n## Run log\n")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POSITION_LEDGER_PATH", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    import core.wallet.authority as auth
    monkeypatch.setattr(auth, "turn_refusal",
                        lambda ctx: None if getattr(ctx, "user_id", None) == "owner-1"
                        else "refused")
    (tmp_path / "project").mkdir()
    return tmp_path


def _write_ledger(home, text):
    (home / "project" / "kb-root-position-ledger.md").write_text(text)


def _db():
    return open_positions.open_positions_db_path()


@pytest.mark.asyncio
async def test_store_only_has_no_ledger_section(home):
    apply_delta("owner-1", PositionDelta("base", MEME, "MEME", 100, 50.0), db_path=_db())
    _write_ledger(home, _ledger(("MEME", MEME, 100)))
    tool = DefiDataTool(price_fn=lambda c, a: _px(1.0))
    res = await tool.positions(PositionsParams(), execution_context=_ctx())
    assert res.error is None, res.error
    assert len(res.metadata["positions"]) == 1
    assert res.metadata["ledger_only"] == [] and res.metadata["ledger_readable"]
    assert "LEDGER-ONLY" not in res.extracted_content


@pytest.mark.asyncio
async def test_ledger_only_is_listed_basis_unknown_and_never_written(home):
    _write_ledger(home, _ledger(("JUGGERNAUT", JUGG, "5000"), ("HOOKR", HOOK, "~1,200"),
                                ("GONE", MEME, "0 — FULL EXIT")))
    tool = DefiDataTool(price_fn=lambda c, a: _px(0.01))
    res = await tool.positions(PositionsParams(chain="robinhood"),
                               execution_context=_ctx())
    assert res.error is None, res.error
    rows = {r["symbol"]: r for r in res.metadata["ledger_only"]}
    assert set(rows) == {"JUGGERNAUT", "HOOKR"}  # the closed row is not open
    j = rows["JUGGERNAUT"]
    assert j["basis_usd"] is None and j["unrealized_usd"] is None
    assert j["qty"] == 5000 and j["value_usd"] == pytest.approx(50.0)
    assert j["chain"] == "robinhood" and j["chain_assumed"] is True
    text = res.extracted_content
    assert "LEDGER-ONLY" in text and "basis unknown" in text
    assert "$12.40" not in text  # the ledger's typed Entry USD is never a basis
    assert "No open position is recorded in the rail book" in text
    # read-only: no store was created, nothing was backfilled
    assert not os.path.exists(_db())


@pytest.mark.asyncio
async def test_ledger_only_without_chain_is_not_priced(home):
    _write_ledger(home, _ledger(("JUGGERNAUT", JUGG, "5000")))
    tool = DefiDataTool(price_fn=lambda c, a: _px(0.01))
    res = await tool.positions(PositionsParams(), execution_context=_ctx())
    (j,) = res.metadata["ledger_only"]
    assert j["chain"] is None and j["price_usd"] is None and j["value_usd"] is None
    assert "pass chain=" in res.extracted_content


@pytest.mark.asyncio
async def test_both_a_store_row_on_any_chain_is_not_doubled(home):
    apply_delta("owner-1", PositionDelta("base", MEME, "MEME", 100, 50.0), db_path=_db())
    _write_ledger(home, _ledger(("MEME", MEME.upper().replace("0X", "0x"), 100),
                                ("JUGGERNAUT", JUGG, 5000)))
    tool = DefiDataTool(price_fn=lambda c, a: _px(1.0))
    # Scoped to another chain: MEME's store row is on base, still not ledger-only.
    res = await tool.positions(PositionsParams(chain="robinhood"),
                               execution_context=_ctx())
    assert res.metadata["positions"] == []
    assert [r["symbol"] for r in res.metadata["ledger_only"]] == ["JUGGERNAUT"]
    res = await tool.positions(PositionsParams(), execution_context=_ctx())
    assert [p["symbol"] for p in res.metadata["positions"]] == ["MEME"]
    assert [r["symbol"] for r in res.metadata["ledger_only"]] == ["JUGGERNAUT"]


@pytest.mark.asyncio
async def test_unreadable_ledger_is_could_not_read_not_empty(home, monkeypatch):
    apply_delta("owner-1", PositionDelta("base", MEME, "MEME", 100, 50.0), db_path=_db())
    monkeypatch.setenv("POSITION_LEDGER_PATH", str(home / "missing-ledger.md"))
    tool = DefiDataTool(price_fn=lambda c, a: _px(1.0))
    res = await tool.positions(PositionsParams(), execution_context=_ctx())
    assert res.error is None  # the store rows still stand
    assert len(res.metadata["positions"]) == 1
    assert res.metadata["ledger_readable"] is False
    assert "could not read" in res.metadata["ledger_state"]
    assert "could not read" in res.extracted_content and "UNKNOWN" in res.extracted_content


@pytest.mark.asyncio
async def test_no_ledger_found_is_said(home):
    tool = DefiDataTool(price_fn=lambda c, a: _px(1.0))
    res = await tool.positions(PositionsParams(), execution_context=_ctx())
    assert res.error is None
    assert "no position ledger found" in res.extracted_content
