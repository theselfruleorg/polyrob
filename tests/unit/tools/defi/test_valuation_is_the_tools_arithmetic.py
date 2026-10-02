"""A money figure the owner reads is computed by a TOOL, never by the model.

⚠️ The defect, from the prod session of 2026-09-29 10:06Z. Asked to "assess
current treasury", the agent read ``defi_data_portfolio``. The budget ran out
before the wallet's OWN buyback token (the loop walked addresses in sorted
order), so the line was::

    0xbBa6…  156387468620751102999533403 raw units (decimals not read)  value NOT READ (budget)

The model converted it in its head, then wrote in its memory
``156,387,468.62 raw units = 156.38746862 tokens (18 dec) ≈ $6.78`` — one
division by 10**6 too many — and told the owner the position was worth "$7"
instead of ~$6,780. Every later check re-used the wrong quantity, and when the
owner caught it the agent invented three different causes.

Three fixes, one per test group below:

1. a low-confidence line shows the indicative value the TOOL computed;
2. canonical and owner-pinned tokens are valued FIRST, before the budget can
   run out on them;
3. a claim prints its worth. (Its gas fee is labelled ``cost:`` since
   6cf2577d0 — see test_launchpad_claim_cost_label.py.)
"""
import pytest

from tools.defi import book, data_tool


class _Ident:
    def __init__(self, symbol, decimals=18, name=None):
        self.symbol, self.decimals, self.name = symbol, decimals, name


class _Price:
    def __init__(self, price_usd, confidence="high"):
        self.price_usd, self.confidence = price_usd, confidence


def _addr(n):
    return "0x" + f"{n:040x}"


def _out(res):
    return (getattr(res, "content", None) or getattr(res, "extracted_content", None)
            or "") + (getattr(res, "error", None) or "")


# --- 1. the indicative value is the tool's ------------------------------------ #

def test_a_low_confidence_line_carries_the_computed_value():
    text = data_tool._excluded_value_text(156_387_468.62, _Price(0.00004335, "low"))
    assert "6,779" in text, text              # 156,387,468.62 × 0.00004335
    assert "$0.00004335" in text
    assert "EXCLUDED" in text and "low" in text


def test_the_indicative_value_is_not_parsed_as_a_confident_one():
    """`book._worth_from_portfolio` reads `= $X` at the line end as a confident
    value. The indicative figure must never be mistaken for it."""
    addr = _addr(7)
    line = (f"  {addr}  156,387,468.620000 PNL  "
            f"{data_tool._excluded_value_text(156_387_468.62, _Price(0.00004335, 'low'))}")
    assert book._worth_from_portfolio(line, addr) is None


def test_no_price_still_says_excluded_without_a_figure():
    text = data_tool._excluded_value_text(10.0, _Price(None, "unknown"))
    assert text == "value EXCLUDED (unknown confidence price)"
    assert data_tool._excluded_value_text(None, _Price(1.0, "low")) == \
        "value EXCLUDED (low confidence price)"


@pytest.mark.asyncio
async def test_portfolio_renders_the_indicative_value_for_a_pinned_token(monkeypatch):
    monkeypatch.setenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", "300")
    monkeypatch.setattr(data_tool, "_pinned_addresses", lambda chain: {_addr(1)})
    tool = data_tool.DefiDataTool(
        holder=_addr(0xABC),
        index_fn=lambda h, chain=None: {_addr(1): 156387468620751102999533403},
        identity_fn=lambda c, a: _Ident("PNL"),
        price_fn=lambda c, a: _Price(0.00004335, "low"),
    )
    tool._gas_lines = lambda *a, **k: []
    out = _out(await tool.portfolio(data_tool.PortfolioParams(chain="base")))
    assert "156,387,468.62" in out
    assert "≈ $6,779" in out, out


@pytest.mark.asyncio
async def test_an_unpinned_token_gets_no_indicative_value(monkeypatch):
    """An airdrop priced by its sender's own pool must not print a fortune."""
    monkeypatch.setenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", "300")
    monkeypatch.setattr(data_tool, "_pinned_addresses", lambda chain: set())
    tool = data_tool.DefiDataTool(
        holder=_addr(0xABC),
        index_fn=lambda h, chain=None: {_addr(1): 10 * 10 ** 18},
        identity_fn=lambda c, a: _Ident("SCAM"),
        price_fn=lambda c, a: _Price(1_000_000.0, "low"),
    )
    tool._gas_lines = lambda *a, **k: []
    out = _out(await tool.portfolio(data_tool.PortfolioParams(chain="base")))
    assert "value EXCLUDED (low confidence price)" in out
    assert "indicative" not in out and "10,000,000" not in out


# --- 2. the tokens we deliberately hold are valued first ---------------------- #

@pytest.mark.asyncio
async def test_a_pinned_token_is_valued_before_the_budget_runs_out(monkeypatch):
    """The pinned address sorts LAST by address; it must still be resolved first."""
    monkeypatch.setenv("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", "10")
    pinned = _addr(0xFFF)
    monkeypatch.setattr(data_tool, "_pinned_addresses", lambda chain: {pinned.lower()})
    # start, first-row check: within budget; everything after: spent.
    ticks = iter([0.0, 0.0] + [1000.0] * 50)
    monkeypatch.setattr(data_tool.time, "monotonic", lambda: next(ticks))
    resolved = []

    def _ident(chain, addr):
        resolved.append(addr)
        return _Ident("PNL")

    tool = data_tool.DefiDataTool(
        holder=_addr(0xABC),
        index_fn=lambda h, chain=None: {_addr(1): 10 ** 18, _addr(2): 10 ** 18,
                                        pinned: 5 * 10 ** 18},
        identity_fn=_ident,
        price_fn=lambda c, a: _Price(2.0),
    )
    tool._gas_lines = lambda *a, **k: []
    out = _out(await tool.portfolio(data_tool.PortfolioParams(chain="base")))
    assert resolved == [pinned], resolved
    assert "$10.00" in out                     # 5 × $2, valued by the tool
    for n in (1, 2):                            # the rest still named
        assert _addr(n) in out


def test_a_raw_units_line_forbids_the_hand_conversion():
    lines = data_tool._budget_exhausted_lines(
        [(_addr(3), None, None, 156387468620751102999533403)], priced=0, total=1)
    row = [ln for ln in lines if _addr(3) in ln][0]
    assert "do not convert by hand" in row


# --- 3. a claim says what it is worth ------------------------------------------ #

def test_the_claim_worth_is_computed_from_the_native_price(monkeypatch):
    from tools.launchpad.tool import LaunchpadTool
    tool = LaunchpadTool(name="launchpad", price_fn=lambda c, a: 2713.0)
    text = tool._native_worth_text(308_734_466_399_179_249)
    assert "≈ $837.60" in text, text
    assert "0.308734" in text


def test_an_unpriced_claim_worth_is_unknown_not_zero():
    from tools.launchpad.tool import LaunchpadTool
    tool = LaunchpadTool(name="launchpad", price_fn=lambda c, a: None)
    text = tool._native_worth_text(10 ** 18)
    assert text.startswith("unknown") and "$0" not in text


def test_an_own_launch_counts_as_ours_without_an_owner_pin(monkeypatch):
    """Prod 2026-09-29: no token_pins.db at all; the buyback token is proven ours
    only by `token_provenance.own_tokens`. It must still be valued first."""
    from core.wallet import token_pins, token_provenance
    monkeypatch.setattr(token_pins, "all_pins", lambda **k: [])
    monkeypatch.setattr(token_provenance, "all_own_tokens", lambda **k: [
        {"chain": "robinhood", "address": "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e",
         "kind": "onchain_probe", "evidence": "pons"},
        {"chain": "base", "address": _addr(5), "kind": "deploy", "evidence": ""}])
    got = data_tool._pinned_addresses("robinhood")
    assert got == {"0xbba60ab93fc409b1a34371cbf6c3173795ed2c7e"}
