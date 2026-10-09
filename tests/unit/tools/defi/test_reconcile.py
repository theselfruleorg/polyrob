"""Ledger ⟷ chain reconciliation — the §0 recall-failure fix.

The incident this guards against, verbatim from 2026-08-25: the ledger's
Open-positions table said "(none — book flat)" while the chain held three
recorded positions, and the agent published the table's side of the story.
Nothing compared the two. These tests pin the comparison.
"""
import pytest

from tools.defi.reconcile import (
    ChainHolding, LedgerPosition, diff, parse_open_positions, render,
)
from tools.defi.data_tool import DefiDataTool, ReconcileParams

BASECAT = "0xB2000000000000000000004c27f6523082f41D01"
BOTS = "0x4DcDF3451dFC114991283c2e5B72823d69882BA3"
BASEUNC = "0xB2000000000000000000000Ff4a547c891AB1b01"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOLMINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

LEDGER = f"""# Treasury Position Ledger — Base

## Open positions

| Token | Address | Size | Entry price | Thesis | Date |
|-------|---------|------|-------------|--------|------|
| Basecat | {BASECAT} | 28.925134 | 0.03456510 | fresh launch | 2026-08-23 |
| **Bots** | {BOTS} | 442,232.10 | 0.00000112 | strongest screen | 2026-08-23 |

## Closed positions

| BPAD | 0xf5F11BC9Be9D6690f795D04d2fc9bdd097008a2B | 10,320.48 | closed |

## Run log

- prose that mentions {BASEUNC} and must NOT be parsed as state
"""

FLAT_LEDGER = """# Ledger

## Open positions

| Token | Address | Size |
|-------|---------|------|
| *(none — book flat as of 2026-08-25 12:13 UTC)* | | |

## Closed positions
"""


def _held(addr, qty, *, symbol="TOK", value=None, known=True, raw=1):
    return ChainHolding(address=addr, symbol=symbol, qty=qty,
                        raw_units=raw if known else None,
                        value_usd=value, balance_known=known)


# --------------------------------------------------------------------------
# parsing — only the STATE TABLE is state
# --------------------------------------------------------------------------

def test_parses_table_rows_with_commas_and_markdown_noise():
    rows, err = parse_open_positions(LEDGER)
    assert err is None
    assert [(r.address, r.qty) for r in rows] == [
        (BASECAT, 28.925134), (BOTS, 442232.10)]
    assert rows[1].symbol == "Bots"


def test_the_run_log_is_never_parsed_as_state():
    rows, _ = parse_open_positions(LEDGER)
    assert all(r.address != BASEUNC for r in rows), \
        "an address mentioned in prose below the table is not an open row"


def test_the_closed_positions_table_is_not_open_state():
    rows, _ = parse_open_positions(LEDGER)
    assert all("f5F11BC9" not in r.address for r in rows)


def test_a_flat_placeholder_row_parses_to_zero_rows():
    rows, err = parse_open_positions(FLAT_LEDGER)
    assert err is None and rows == []


def test_a_missing_section_is_an_error_not_an_empty_book():
    rows, err = parse_open_positions("# nothing here\n\njust prose\n")
    assert rows == [] and err is not None and "Open positions" in err


def test_an_unparseable_size_still_yields_the_row():
    text = f"## Open positions\n| X | {BASECAT} | soon™ | | |\n"
    rows, _ = parse_open_positions(text)
    assert len(rows) == 1 and rows[0].qty is None


# --------------------------------------------------------------------------
# diff — the incident, in both directions
# --------------------------------------------------------------------------

def test_the_2026_08_25_incident_is_caught():
    """Table says flat; chain holds three positions -> a DISAGREEMENT verdict.

    071 W3: the two PRICED holdings are unexplained by the ledger alone; the
    UNPRICED one is only caught when the rail store says the rail bought it —
    without the store an unpriced token in neither book is `unsolicited`."""
    from tools.defi.reconcile import RailPosition
    holdings = [_held(BASECAT, 28.925134, value=0.89),
                _held(BOTS, 442232.099224, value=1.26),
                _held(BASEUNC, 6359.802880, value=None)]
    report = diff([], holdings, quote_addresses=[USDC])
    assert report.verdict == "DISAGREEMENT"
    assert len(report.unexplained) == 2 and len(report.unsolicited) == 1
    rail = [RailPosition(address=a, symbol="TOK", qty=q) for a, q in
            ((BASECAT, 28.925134), (BOTS, 442232.099224), (BASEUNC, 6359.802880))]
    report = diff([], holdings, quote_addresses=[USDC], rail=rail)
    assert report.verdict == "DISAGREEMENT"
    assert len(report.unexplained) == 3 and not report.unsolicited
    assert all("[rail store, not in ledger]" in u for u in report.unexplained)


def test_the_reverse_incident_is_caught_too():
    """Ledger row open, chain balance zero (the resurrected-BPAD shape)."""
    rows, _ = parse_open_positions(LEDGER)
    report = diff(rows, [_held(BASECAT, 28.925134, value=0.9)],
                  quote_addresses=[USDC])
    assert any(BOTS in item for item in report.unbacked)
    assert report.verdict == "DISAGREEMENT"


def test_a_clean_book_is_clean():
    rows, _ = parse_open_positions(LEDGER)
    holdings = [_held(BASECAT, 28.925134, value=0.89),
                _held(BOTS, 442232.099224, value=1.26),
                _held(USDC, 11.24, symbol="USDC", value=11.24)]
    report = diff(rows, holdings, quote_addresses=[USDC])
    assert report.verdict == "CLEAN", (report.unbacked, report.unexplained,
                                       report.mismatched)
    assert len(report.matched) == 2


def test_rounding_within_tolerance_matches():
    """The ledger records 442,232.10 for an on-chain 442,232.099224."""
    rows, _ = parse_open_positions(LEDGER)
    report = diff(rows, [_held(BASECAT, 28.925134),
                         _held(BOTS, 442232.099224)])
    assert not report.mismatched


def test_a_real_size_divergence_is_flagged():
    rows, _ = parse_open_positions(LEDGER)
    report = diff(rows, [_held(BASECAT, 14.0), _held(BOTS, 442232.10)])
    assert any(BASECAT in item for item in report.mismatched)


def test_a_failed_read_is_unknown_never_zero():
    rows, _ = parse_open_positions(LEDGER)
    holdings = [_held(BASECAT, None, known=False),
                _held(BOTS, 442232.10)]
    report = diff(rows, holdings)
    assert not report.unbacked, "a read failure must NOT read as 'position gone'"
    assert any(BASECAT in item for item in report.unknown)
    assert report.verdict == "UNVERIFIED"


def test_quote_asset_is_working_capital_not_an_unexplained_holding():
    report = diff([], [_held(USDC, 11.24, symbol="USDC", value=11.24)],
                  quote_addresses=[USDC])
    assert report.unexplained == [] and report.verdict == "CLEAN"


def test_confidently_priced_dust_is_noise_not_a_disagreement():
    report = diff([], [_held("0x" + "9" * 40, 5000.0, symbol="GOOK",
                             value=0.03)])
    assert report.unexplained == [] and len(report.dust) == 1
    assert report.verdict == "CLEAN"


def test_unpriced_holding_in_neither_book_is_unsolicited_not_a_disagreement():
    """071 W3 (TM P0-7): an unpriced airdrop in neither book used to read
    `unexplained` forever, so a dusted wallet could never be CLEAN and the
    agent learned to ignore the verdict. Now it is `unsolicited` — named, not
    an issue. A rail-tracked unpriced holding the ledger omits is still a
    disagreement: a missing price must not hide a missing row."""
    from tools.defi.reconcile import RailPosition
    report = diff([], [_held(BASEUNC, 6359.80, value=None)])
    assert report.unexplained == [] and len(report.unsolicited) == 1
    assert report.verdict == "CLEAN"
    report = diff([], [_held(BASEUNC, 6359.80, value=None)],
                  rail=[RailPosition(address=BASEUNC, symbol="BU", qty=6359.80)])
    assert len(report.unexplained) == 1 and report.verdict == "DISAGREEMENT"


def test_a_zero_balance_is_not_a_holding():
    """The live run listed BPAD 0.000000 as 'held on chain' — an emptied token
    account the indexer still returns. Zero is not a holding."""
    report = diff([], [_held(BASECAT, 0.0, raw=0)])
    assert report.unexplained == [] and report.verdict == "CLEAN"


def test_the_unexplained_guidance_is_said_once_not_per_row():
    """The first live run repeated a three-sentence instruction 21 times —
    the guidance lives in the section header, rows stay one line."""
    report = diff([], [_held(BASECAT, 1.0, value=1.0),
                       _held(BOTS, 2.0, value=2.0)])
    out = render(report, chain="base", holder="0xabc", ledger_path="x.md",
                 coverage="indexed")
    assert out.count("decide and write it down NOW") == 1


def test_a_solana_row_is_skipped_on_an_evm_reconcile_not_lied_about():
    rows = [LedgerPosition(symbol="Q", address=SOLMINT, qty=5.0, line="|Q|")]
    report = diff(rows, [], evm_chain=True)
    assert report.unbacked == []
    assert len(report.skipped_other_family) == 1


def test_render_states_authority_and_verdict_first():
    report = diff([], [_held(BASECAT, 1.0, value=1.0)])
    out = render(report, chain="base", holder="0xabc", ledger_path="x.md",
                 coverage="indexed")
    assert "DISAGREEMENT" in out
    assert "authoritative" in out
    assert out.index("VERDICT") < out.index("HELD ON CHAIN")


# --------------------------------------------------------------------------
# the action — file confinement + end to end through the seams
# --------------------------------------------------------------------------

def _text(res):
    return res.extracted_content or ""


@pytest.mark.asyncio
async def test_action_end_to_end_catches_the_incident(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ledger = tmp_path / "project" / "kb-root-position-ledger.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(FLAT_LEDGER)
    tool = DefiDataTool(
        holder="0xHOLDER",
        index_fn=lambda holder, chain: {BASECAT: 28925134, BOTS: 442232099224},
        identity_fn=lambda chain, addr: type(
            "I", (), {"symbol": "TOK", "decimals": 6})(),
        price_fn=lambda chain, addr: type(
            "P", (), {"price_usd": 1.0, "confidence": "high"})(),
    )
    out = _text(await tool.reconcile(ReconcileParams(
        chain="base", ledger_path=str(ledger))))
    assert "DISAGREEMENT" in out
    assert "NO open-positions row" in out or "NOT IN THE LEDGER" in out
    assert "authoritative" in out


@pytest.mark.asyncio
async def test_action_resolves_a_relative_path_against_the_workspace_not_cwd(
        tmp_path, monkeypatch):
    """2026-08-28 live bug: a relative ledger_path resolved against the
    SERVICE's cwd (Path.cwd(), e.g. /opt/polyrob) instead of the session's
    workspace — the same bare filename filesystem_read_file/write_file
    resolve fine against execution_context.workspace_dir. Every treasury run
    burned an extra step guessing the absolute path after this 404'd first."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "kb-root-position-ledger.md").write_text(FLAT_LEDGER)
    # cwd is deliberately somewhere that does NOT contain the ledger (but is
    # still a valid confinement root via POLYROB_DATA_DIR, unrelated to cwd),
    # so a cwd-relative resolution would 404 and only workspace_dir-relative
    # resolution finds the file.
    elsewhere = tmp_path / "elsewhere-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    execution_context = type("Ctx", (), {"workspace_dir": str(workspace)})()
    res = await tool.reconcile(ReconcileParams(
        chain="base", ledger_path="kb-root-position-ledger.md"),
        execution_context=execution_context)
    assert res.error is None, res.error


@pytest.mark.asyncio
async def test_action_refuses_a_path_outside_the_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    outside = tmp_path / "elsewhere.md"
    outside.write_text(FLAT_LEDGER)
    monkeypatch.chdir(tmp_path / "home")
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    res = await tool.reconcile(ReconcileParams(
        chain="base", ledger_path=str(outside)))
    assert res.error and "outside" in res.error.lower()


@pytest.mark.asyncio
async def test_action_missing_ledger_is_an_error_not_a_clean_book(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    res = await tool.reconcile(ReconcileParams(
        chain="base", ledger_path=str(tmp_path / "missing.md")))
    assert res.error and "not found" in res.error.lower()


@pytest.mark.asyncio
async def test_action_reconciles_solana_rather_than_refusing_it(tmp_path, monkeypatch):
    """Superseded 2026-08-28. This verb used to refuse non-EVM chains and tell
    the agent to "compare it by hand" — hand-comparison from memory being the
    precise recall failure reconcile was built to replace, on a chain the agent
    can now trade. It reconciles Solana through the same comparison; only the
    balance SOURCE differs (getTokenAccountsByOwner, no indexer key)."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ledger = tmp_path / "ledger.md"
    ledger.write_text(FLAT_LEDGER)
    tool = DefiDataTool(holder="0xHOLDER")
    tool._solana_holder = lambda: "Brs1111111111111111111111111111111111111111"
    tool._solana_tokens = lambda h: {}
    res = await tool.reconcile(ReconcileParams(
        chain="solana", ledger_path=str(ledger)))
    assert res.error is None, res.error
    assert "chain solana" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_a_solana_book_cannot_render_clean_over_unchecked_rows(tmp_path, monkeypatch):
    """`diff(evm_chain=True)` on a Solana book filed every base58 row as
    "other chain family (not checked here)" and returned CLEAN — a false
    all-clear over an unreconciled book, the worst output this verb can give."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "## Open positions\n\n| Sym | Address | Size |\n|---|---|---|\n"
        f"| JOY | {SOLMINT} | 1000.0 |\n")
    tool = DefiDataTool(
        holder="0xHOLDER",
        identity_fn=lambda c, a: type("I", (), {
            "symbol": "TOK", "name": "T", "decimals": 0,
            "verified": False, "metadata_changed": False})(),
        price_fn=lambda c, a: type("P", (), {
            "price_usd": 1.0, "confidence": "high"})())
    tool._solana_holder = lambda: "Brs1111111111111111111111111111111111111111"
    tool._solana_tokens = lambda h: {}
    out = _text(await tool.reconcile(ReconcileParams(
        chain="solana", ledger_path=str(ledger))))
    assert "VERDICT: CLEAN" not in out
    assert "not checked here" not in out


# --- 2026-09-21: a refused gate is a durable FACT, not a log line ----------------
#
# The money rails' step 1 is this verb ("if a real row disagrees, STOP"). On
# 2026-09-21 it refused every call for 9 h (the ledger had grown past 1 MB) and
# six rails ran without their gate while every status seat read healthy. A
# refusal now emits `rail_precondition_failed{tool: reconcile, reason}` so the
# status snapshot can render a CRIT (tests/unit/core/test_status_snapshot.py).

def _rows(kind):
    from core.event_log import get_event_log
    return [r for r in get_event_log().query(limit=50) if r.get("kind") == kind]


@pytest.mark.asyncio
async def test_a_refused_reconcile_is_recorded_as_a_failed_precondition(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    big = tmp_path / "kb-root-position-ledger.md"
    big.write_text(("x" * 1_000_001) + LEDGER)  # the table is NOT in the first 1 MB
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    ctx = type("Ctx", (), {"user_id": "u1", "session_id": "s-1",
                           "workspace_dir": str(tmp_path)})()
    res = await tool.reconcile(ReconcileParams(chain="base", ledger_path=str(big)),
                               execution_context=ctx)
    assert res.error and "exceeds 1MB" in res.error
    rows = _rows("rail_precondition_failed")
    assert len(rows) == 1
    a = rows[0]["attrs"]
    assert a["tool"] == "reconcile" and a["chain"] == "base"
    assert "exceeds 1MB" in a["reason"]
    assert rows[0]["user_id"] == "u1" and rows[0]["session_id"] == "s-1"


@pytest.mark.asyncio
async def test_a_clean_reconcile_records_no_failed_precondition(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    (tmp_path / "ledger.md").write_text(FLAT_LEDGER)
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    res = await tool.reconcile(ReconcileParams(chain="base",
                                               ledger_path=str(tmp_path / "ledger.md")))
    assert res.error is None, res.error
    assert _rows("rail_precondition_failed") == []


@pytest.mark.asyncio
async def test_a_long_run_log_tail_does_not_disable_the_gate(tmp_path, monkeypatch):
    """2026-09-21: the ledger passed 1 MB because every rail appends its run log
    BELOW the tables, and the whole-file size cap then refused the money rails'
    step-1 gate for 9 h. The state table lives at the top; only that section is
    ever parsed. So the cap now bounds the HEAD that must contain the table, not
    the file: a legitimate ledger with a long narrative tail reconciles, while a
    file whose first 1 MB holds no `## Open positions` section still refuses."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    led = tmp_path / "kb-root-position-ledger.md"
    led.write_text(FLAT_LEDGER + "\n## Run log\n" + ("- narrative line\n" * 80_000))
    assert led.stat().st_size > 1_000_000
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    res = await tool.reconcile(ReconcileParams(chain="base", ledger_path=str(led)))
    assert res.error is None, res.error
    assert _rows("rail_precondition_failed") == []


# --------------------------------------------------------------------------
# 2026-10-03: the gate timed out on every call. The treasury holds 80+ tokens
# (mostly airdropped dust) and the chain side priced each one with its own
# per-source call; GeckoTerminal answered 429 to each, slept 2 s and retried,
# so the read passed the 60 s action budget and every buyback cycle STOPPED at
# step 1. Reconcile now takes the holdings loop's batched prefetch.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_prices_the_chain_side_from_one_batch_prefetch(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ledger = tmp_path / "project" / "kb-root-position-ledger.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(LEDGER)
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
    out = _text(await tool.reconcile(ReconcileParams(chain="base", ledger_path=str(ledger))))

    assert "authoritative" in out
    assert len(prefetch_calls) == 1
    assert prefetch_calls[0] == sorted(held)
    assert sorted(per_row) == sorted(held)
    # A ledger position is one we hold on purpose: its primary source is always
    # asked; airdropped dust may be answered from the batch alone.
    assert ask_primary[BASECAT] is True and ask_primary[BOTS] is True
    assert not any(ask_primary[a] for a in dust)


@pytest.mark.asyncio
async def test_reconcile_values_holdings_concurrently_and_keeps_their_order(tmp_path, monkeypatch):
    """116 holdings x (3 identity eth_calls + a price quote) ran one after another:
    93 s measured on prod after the batch fix, past the 60 s action budget. The
    per-row reads are independent, so they run on a small pool; the report is
    unchanged and still in address order."""
    import time as _time
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ledger = tmp_path / "project" / "kb-root-position-ledger.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(LEDGER)
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
    res = await tool.reconcile(ReconcileParams(chain="base", ledger_path=str(ledger)))
    elapsed = _time.monotonic() - t0
    assert elapsed < 2.4, f"24 rows x 0.2 s ran serially ({elapsed:.1f}s)"
    out = _text(res)
    dust_lines = [ln for ln in out.splitlines() if "dust/airdrop" in ln]
    addrs = [a for a in held if any(a in ln for ln in dust_lines)]
    assert addrs == sorted(held)
