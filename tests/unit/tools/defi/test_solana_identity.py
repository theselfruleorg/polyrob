"""Solana token identity — the treasury's own USDC must not read as spam.

Live prod evidence (2026-08-28): portfolio(chain='solana') filed the agent's
own USDC under "unvalued — NOT necessarily holdings you bought ... treat as
noise, never as a position", because `_identity` short-circuited every non-EVM
chain to decimals=None before the pinned table was ever consulted, and
`_canonical_pins` only walked `evm_rows()`. Unknown-decimals is the correct
answer for an UNKNOWN mint; it is a lie for the mint the x402 settlement rail
already asset-pins.
"""
import pytest

SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
RANDOM_MINT = "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E"


def test_solana_usdc_is_pinned_with_six_decimals():
    from core.wallet.tokens import canonical_token
    pin = canonical_token("solana", SOL_USDC)
    assert pin and pin["symbol"] == "USDC" and pin["decimals"] == 6


def test_wrapped_sol_is_pinned_with_nine_decimals_not_eighteen():
    """The EVM pin builder hardcoded 18. wSOL is 9 — an 18 here would size a
    swap a billion times wrong."""
    from core.wallet.tokens import canonical_token
    pin = canonical_token("solana", WSOL)
    assert pin and pin["decimals"] == 9 and pin["symbol"] == "WSOL"


def test_an_unpinned_solana_mint_still_reports_unknown():
    """The fix must not invent decimals for a mint nobody verified."""
    from tools.defi.data_tool import DefiDataTool
    ident = DefiDataTool()._identity("solana", RANDOM_MINT)
    assert ident.decimals is None


def test_identity_resolves_pinned_solana_mints():
    from tools.defi.data_tool import DefiDataTool
    ident = DefiDataTool()._identity("solana", SOL_USDC)
    assert ident.decimals == 6 and ident.symbol == "USDC"


@pytest.mark.asyncio
async def test_portfolio_values_solana_usdc_instead_of_calling_it_spam():
    from tools.defi.data_tool import DefiDataTool, PortfolioParams
    tool = DefiDataTool(
        holder="Brs1111111111111111111111111111111111111111",
        price_fn=lambda c, a: type("P", (), {
            "price_usd": 1.0, "confidence": "high", "pool_count": 9,
            "liquidity_usd": 1e7})(),
    )
    tool._solana_tokens = lambda h: {SOL_USDC: 100000}
    tool._solana_native = lambda h: 0
    res = await tool.portfolio(PortfolioParams(chain="solana"))
    text = res.extracted_content or ""
    assert "raw units" not in text
    assert "0.100000" in text
    # It must be counted, not parked in the airdrop-spam block.
    assert "$0.10" in text


# --------------------------------------------------------------------------
# reconcile — the ledger/chain check must cover the Solana book too
# --------------------------------------------------------------------------
# `reconcile` exists because on 2026-08-25 the agent held three positions
# on-chain, its ledger said "Open positions: NONE", and it published the wrong
# side of that disagreement. Refusing non-EVM chains left the Solana book
# exposed to the identical failure, and told the agent to "compare it by hand"
# — the exact manual recall the verb was built to replace.

_SOL_LEDGER = """# Position ledger

## Open positions

| Symbol | Address | Size | Entry | Thesis |
|---|---|---|---|---|
| JOY | 9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E | 1000.0 | $0.001 | fresh pump-fun launch |
"""


def _sol_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    led = tmp_path / "project" / "kb-root-position-ledger.md"
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text(_SOL_LEDGER)
    return led


def _sol_tool(held):
    from tools.defi.data_tool import DefiDataTool
    tool = DefiDataTool(
        holder="Brs1111111111111111111111111111111111111111",
        price_fn=lambda c, a: type("P", (), {
            "price_usd": 1.0, "confidence": "high", "pool_count": 9,
            "liquidity_usd": 1e7})(),
        identity_fn=lambda c, a: type("I", (), {
            "symbol": "TOK", "name": "Token", "decimals": 0,
            "verified": False, "metadata_changed": False})(),
    )
    tool._solana_holder = lambda: "Brs1111111111111111111111111111111111111111"
    tool._solana_tokens = lambda h: held
    return tool


@pytest.mark.asyncio
async def test_reconcile_accepts_solana(tmp_path, monkeypatch):
    from tools.defi.data_tool import ReconcileParams
    led = _sol_ledger(tmp_path, monkeypatch)
    tool = _sol_tool({RANDOM_MINT: 1000})
    res = await tool.reconcile(ReconcileParams(chain="solana",
                                               ledger_path=str(led)))
    assert res.error is None, res.error
    assert "EVM chains only" not in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_reconcile_catches_an_unbacked_solana_ledger_row(tmp_path, monkeypatch):
    """Ledger claims JOY; the chain holds none of it."""
    from tools.defi.data_tool import ReconcileParams
    led = _sol_ledger(tmp_path, monkeypatch)
    tool = _sol_tool({})
    res = await tool.reconcile(ReconcileParams(chain="solana",
                                               ledger_path=str(led)))
    assert "DISAGREEMENT" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_reconcile_reports_a_failed_solana_read_as_unknown_not_zero(tmp_path, monkeypatch):
    """A failed token-account read must never read as 'the position is gone'."""
    from tools.defi.data_tool import ReconcileParams
    led = _sol_ledger(tmp_path, monkeypatch)
    tool = _sol_tool(None)
    res = await tool.reconcile(ReconcileParams(chain="solana",
                                               ledger_path=str(led)))
    text = res.extracted_content or ""
    assert "DISAGREEMENT" not in text
    assert "UNAVAIL" in text.upper() or "UNVERIFIED" in text.upper()


def test_the_wsol_mint_has_exactly_one_source():
    """address IS identity on Solana, and base58 carries no checksum — two
    copies of a mint that must agree is a silent drift risk."""
    from core.wallet import chains
    from tools.defi.trade_tool import _WSOL_MINT
    assert _WSOL_MINT == chains.get("solana").wrapped_native == WSOL


@pytest.mark.asyncio
async def test_swap_quote_on_solana_names_the_verb_that_does_quote_there():
    """swap_quote drives the EVM route providers, and solana's route_hints are
    empty by design (Jupiter routes it inside defi_trade.solana_swap). Saying
    "no route on solana" to an agent told to "quote it FIRST" re-teaches the
    exact false belief that Solana is unbuyable — the bug this whole change set
    is about. Point it at the verb that DOES quote there instead."""
    from tools.defi.data_tool import DefiDataTool, SwapQuoteParams
    res = await DefiDataTool().swap_quote(SwapQuoteParams(
        chain="solana", token_in=SOL_USDC, token_out=WSOL, amount_in=1.0))
    text = (res.extracted_content or "") + (res.error or "")
    assert "solana_swap" in text
    assert "dry_run" in text
    # The misleading ASSERTION, not the mere phrase — the message may (and
    # does) name "no route" in order to deny it.
    assert "cannot route" not in text.lower()
