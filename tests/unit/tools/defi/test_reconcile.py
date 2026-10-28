"""Book ⟷ chain reconciliation — the §0 recall-failure fix.

The incident this guards against, from 2026-08-25: the recorded positions said
"book flat" while the chain held three positions, and the agent published the
record's side of the story. Nothing compared the two. These tests pin the
comparison of the rail book (``core/open_positions.py``) against the chain.
"""
import pytest

from core import open_positions
from core.open_positions import PositionDelta, apply_delta
from tools.defi.reconcile import ChainHolding, RailPosition, diff, render
from tools.defi.data_tool import DefiDataTool, ReconcileParams

BASECAT = "0xB2000000000000000000004c27f6523082f41D01"
BOTS = "0x4DcDF3451dFC114991283c2e5B72823d69882BA3"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOLMINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

BOOK = [RailPosition(address=BASECAT, symbol="Basecat", qty=28.925134),
        RailPosition(address=BOTS, symbol="Bots", qty=442232.10)]


def _held(addr, qty, *, symbol="TOK", value=None, known=True, raw=1):
    return ChainHolding(address=addr, symbol=symbol, qty=qty,
                        raw_units=raw if known else None,
                        value_usd=value, balance_known=known)


# --------------------------------------------------------------------------
# the diff
# --------------------------------------------------------------------------

def test_an_unrecorded_holding_is_caught():
    """The 2026-08-25 shape: the book is flat, the chain holds a position."""
    report = diff([], [_held(BASECAT, 28.9, value=1.0), _held(BOTS, 442232.1, value=5.0)])
    assert report.verdict == "DISAGREEMENT"
    assert len(report.unexplained) == 2


def test_a_book_row_the_chain_does_not_back_is_caught():
    report = diff(BOOK, [_held(BASECAT, 28.925134, value=1.0)])
    assert report.verdict == "DISAGREEMENT"
    assert any(BOTS in u for u in report.unbacked)


def test_a_clean_book_is_clean():
    report = diff(BOOK, [_held(BASECAT, 28.925134, value=1.0),
                         _held(BOTS, 442232.099224, value=0.5)],
                  quote_addresses=[USDC])
    assert report.verdict == "CLEAN" and len(report.matched) == 2
    assert report.rail_rows == 2


def test_a_real_size_divergence_is_flagged():
    report = diff(BOOK[:1], [_held(BASECAT, 20.0, value=1.0)])
    assert report.mismatched and report.verdict == "DISAGREEMENT"


def test_a_failed_read_is_unknown_never_zero():
    report = diff(BOOK[:1], [_held(BASECAT, None, known=False)])
    assert report.unknown and not report.unbacked
    assert report.verdict == "UNVERIFIED"


def test_quote_asset_is_working_capital_not_an_unexplained_holding():
    report = diff([], [_held(USDC, 50.0, value=50.0)], quote_addresses=[USDC])
    assert report.verdict == "CLEAN" and not report.unexplained


def test_confidently_priced_dust_is_noise_not_a_disagreement():
    report = diff([], [_held(BASECAT, 1.0, value=0.01)])
    assert report.dust and report.verdict == "CLEAN"


def test_unpriced_holding_not_in_the_book_is_unsolicited_not_a_disagreement():
    report = diff([], [_held(BASECAT, 1000.0, value=None)])
    assert report.unsolicited and report.verdict == "CLEAN"


def test_a_zero_balance_is_not_a_holding():
    report = diff([], [_held(BASECAT, 0.0, raw=0, value=None)])
    assert report.verdict == "CLEAN" and not report.unsolicited


def test_a_solana_row_is_not_compared_on_an_evm_chain():
    report = diff([RailPosition(address=SOLMINT, symbol="JOY", qty=1.0)], [])
    assert report.verdict == "CLEAN" and report.rail_rows == 0


def test_a_quarantined_row_explains_its_holding_and_is_not_compared():
    rail = [RailPosition(address=BASECAT, symbol="LOOK", qty=5.0, status="quarantined")]
    report = diff(rail, [_held(BASECAT, 5.0, value=None)])
    assert any("quarantined" in m for m in report.matched)
    assert report.unsolicited == []


def test_an_unbacked_row_names_the_owner_verb_that_closes_it():
    report = diff(BOOK[:1], [], chain="base")
    assert f"/writeoff base {BASECAT}" in report.unbacked[0]


def test_render_states_authority_and_verdict_first():
    report = diff(BOOK, [])
    text = render(report, chain="base", holder="0xH", coverage="complete")
    assert "VERDICT: DISAGREEMENT" in text
    assert "authoritative" in text
    assert "2 row(s) compared" in text


# --------------------------------------------------------------------------
# the action — end to end through the seams
# --------------------------------------------------------------------------

def _text(res):
    return res.extracted_content or ""


def _ctx(uid="owner-1", session_id="s-1"):
    return type("Ctx", (), {"user_id": uid, "session_id": session_id})()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


def _book(uid="owner-1"):
    db = open_positions.open_positions_db_path()
    apply_delta(uid, PositionDelta("base", BASECAT, "Basecat", 28.925134, 10.0), db_path=db)
    apply_delta(uid, PositionDelta("base", BOTS, "Bots", 442232.10, 5.0), db_path=db)


@pytest.mark.asyncio
async def test_action_end_to_end_catches_an_unrecorded_holding(home):
    tool = DefiDataTool(
        holder="0xHOLDER",
        index_fn=lambda holder, chain: {BASECAT: 28925134, BOTS: 442232099224},
        identity_fn=lambda chain, addr: type("I", (), {"symbol": "TOK", "decimals": 6})(),
        price_fn=lambda chain, addr: type("P", (), {"price_usd": 1.0, "confidence": "high"})(),
    )
    out = _text(await tool.reconcile(ReconcileParams(chain="base"), execution_context=_ctx()))
    assert "DISAGREEMENT" in out
    assert "NOT IN THE BOOK" in out
    assert "authoritative" in out


@pytest.mark.asyncio
async def test_action_reads_the_tenants_book(home):
    _book()
    tool = DefiDataTool(
        holder="0xHOLDER",
        index_fn=lambda holder, chain: {BASECAT: 28925134, BOTS: 442232099224},
        identity_fn=lambda chain, addr: type("I", (), {"symbol": "TOK", "decimals": 6})(),
        price_fn=lambda chain, addr: type("P", (), {"price_usd": 1.0, "confidence": "high"})(),
    )
    res = await tool.reconcile(ReconcileParams(chain="base"), execution_context=_ctx())
    assert res.error is None, res.error
    assert res.metadata["report"]["verdict"] == "CLEAN"
    assert "2 tracked row(s) on base compared" in _text(res)


@pytest.mark.asyncio
async def test_a_book_row_the_index_does_not_list_is_read_directly(home):
    _book()
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {},
                        identity_fn=lambda c, a: type("I", (), {"symbol": "T",
                                                               "decimals": 0})(),
                        price_fn=lambda c, a: type("P", (), {"price_usd": 1.0,
                                                             "confidence": "high"})())
    res = await tool.reconcile(ReconcileParams(chain="base"), execution_context=_ctx())
    assert res.error is None, res.error
    assert len(res.metadata["report"]["unbacked"]) == 2


@pytest.mark.asyncio
async def test_an_unreadable_book_is_an_error_not_a_clean_book(home):
    (home / "open_positions.db").write_text("not a database")
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    res = await tool.reconcile(ReconcileParams(chain="base"), execution_context=_ctx())
    assert res.error and "NOT an empty" in res.error


@pytest.mark.asyncio
async def test_a_stale_ledger_path_argument_is_ignored(home):
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    res = await tool.reconcile(
        ReconcileParams(chain="base", ledger_path="kb-root-position-ledger.md"),
        execution_context=_ctx())
    assert res.error is None, res.error


@pytest.mark.asyncio
async def test_action_reconciles_solana_rather_than_refusing_it(home):
    """Solana is reconciled through the same comparison; only the balance SOURCE
    differs (getTokenAccountsByOwner, no indexer key)."""
    tool = DefiDataTool(holder="0xHOLDER")
    tool._solana_holder = lambda: "Brs1111111111111111111111111111111111111111"
    tool._solana_tokens = lambda h: {}
    res = await tool.reconcile(ReconcileParams(chain="solana"), execution_context=_ctx())
    assert res.error is None, res.error
    assert "chain solana" in _text(res)


@pytest.mark.asyncio
async def test_a_solana_book_cannot_render_clean_over_unchecked_rows(home):
    """`diff(evm_chain=True)` on a Solana book skipped every base58 row and
    returned CLEAN — a false all-clear over an unreconciled book."""
    apply_delta("owner-1", PositionDelta("solana", SOLMINT, "JOY", 1000.0, 1.0),
                db_path=open_positions.open_positions_db_path())
    tool = DefiDataTool(
        holder="0xHOLDER",
        identity_fn=lambda c, a: type("I", (), {
            "symbol": "TOK", "name": "T", "decimals": 0,
            "verified": False, "metadata_changed": False})(),
        price_fn=lambda c, a: type("P", (), {"price_usd": 1.0, "confidence": "high"})())
    tool._solana_holder = lambda: "Brs1111111111111111111111111111111111111111"
    tool._solana_tokens = lambda h: {}
    out = _text(await tool.reconcile(ReconcileParams(chain="solana"),
                                     execution_context=_ctx()))
    assert "VERDICT: CLEAN" not in out
    assert "BOOK ROWS THE CHAIN DOES NOT BACK" in out


# --- 2026-09-21: a refused gate is a durable FACT, not a log line ----------------

def _rows(kind):
    from core.event_log import get_event_log
    return [r for r in get_event_log().query(limit=50) if r.get("kind") == kind]


@pytest.mark.asyncio
async def test_a_refused_reconcile_is_recorded_as_a_failed_precondition(home):
    (home / "open_positions.db").write_text("not a database")
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    res = await tool.reconcile(ReconcileParams(chain="base"), execution_context=_ctx())
    assert res.error
    rows = _rows("rail_precondition_failed")
    assert len(rows) == 1
    a = rows[0]["attrs"]
    assert a["tool"] == "reconcile" and a["chain"] == "base"
    assert "NOT an empty" in a["reason"]
    assert rows[0]["user_id"] == "owner-1" and rows[0]["session_id"] == "s-1"


@pytest.mark.asyncio
async def test_a_clean_reconcile_records_no_failed_precondition(home):
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    res = await tool.reconcile(ReconcileParams(chain="base"), execution_context=_ctx())
    assert res.error is None, res.error
    assert _rows("rail_precondition_failed") == []


# --------------------------------------------------------------------------
# 2026-10-03: the gate timed out on every call (80+ dust tokens priced one by
# one). Reconcile takes the holdings loop's batched prefetch.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_prices_the_chain_side_from_one_batch_prefetch(home, monkeypatch):
    _book()
    dust = [f"0x{i:040x}" for i in range(1, 41)]
    held = {BASECAT: 28925134, BOTS: 442232099224, **{a: 5 for a in dust}}

    from tools.defi import price_sources
    prefetch_calls, per_row, ask_primary = [], [], {}
    sentinel = object()

    def _prefetch(chain, addresses):
        prefetch_calls.append(sorted(addresses))
        return sentinel

    def _quote_prefetched(chain, address, pre, *, must_ask_primary=False):
        assert pre is sentinel
        per_row.append(address)
        ask_primary[address] = must_ask_primary
        return type("P", (), {"price_usd": 1.0, "confidence": "high", "failed": ()})()

    def _no_unbatched_quote(chain, address, **kw):
        raise AssertionError(f"unbatched per-token quote for {address}")

    monkeypatch.setattr(price_sources, "prefetch", _prefetch)
    monkeypatch.setattr(price_sources, "quote_prefetched", _quote_prefetched)
    monkeypatch.setattr(price_sources, "quote", _no_unbatched_quote)

    tool = DefiDataTool(
        holder="0xHOLDER",
        index_fn=lambda holder, chain: dict(held),
        identity_fn=lambda chain, addr: type("I", (), {"symbol": "TOK", "decimals": 6})(),
    )
    out = _text(await tool.reconcile(ReconcileParams(chain="base"), execution_context=_ctx()))

    assert "authoritative" in out
    assert len(prefetch_calls) == 1
    assert prefetch_calls[0] == sorted(held)
    assert sorted(per_row) == sorted(held)
    # A book position is one we hold on purpose: its primary source is always
    # asked; airdropped dust may be answered from the batch alone.
    assert ask_primary[BASECAT] is True and ask_primary[BOTS] is True
    assert not any(ask_primary[a] for a in dust)


@pytest.mark.asyncio
async def test_reconcile_values_holdings_concurrently_and_keeps_their_order(home):
    """The per-row reads are independent, so they run on a small pool; the
    report is unchanged and still in address order."""
    import time as _time
    held = {f"0x{i:040x}": 5 for i in range(1, 25)}

    def _slow_price(chain, addr):
        _time.sleep(0.2)
        return type("P", (), {"price_usd": 1e-9, "confidence": "high", "failed": ()})()

    tool = DefiDataTool(
        holder="0xHOLDER",
        index_fn=lambda holder, chain: dict(held),
        identity_fn=lambda chain, addr: type("I", (), {"symbol": "TOK", "decimals": 6})(),
        price_fn=_slow_price,
    )
    t0 = _time.monotonic()
    res = await tool.reconcile(ReconcileParams(chain="base"), execution_context=_ctx())
    elapsed = _time.monotonic() - t0
    assert elapsed < 2.4, f"24 rows x 0.2 s ran serially ({elapsed:.1f}s)"
    out = _text(res)
    dust_lines = [ln for ln in out.splitlines() if "dust/airdrop" in ln]
    addrs = [a for a in held if any(a in ln for ln in dust_lines)]
    assert addrs == sorted(held)


def test_086_a_major_book_row_is_carried_not_compared_by_size():
    """086 W0: a WETH/wSOL row carries the major's cost basis. Gas, wraps and
    native-vs-wrapped arrival move its size, so the chain is not asked to back
    it — but the row is never filed as unbacked."""
    weth = "0x4200000000000000000000000000000000000006"
    report = diff([RailPosition(address=weth, symbol="WETH", qty=0.03)], [],
                  quote_addresses=[USDC, weth], major_addresses=[weth])
    assert report.verdict == "CLEAN" and not report.unbacked
    assert any("size not compared" in m for m in report.matched)
