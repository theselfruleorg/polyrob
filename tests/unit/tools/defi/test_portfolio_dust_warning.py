"""`portfolio`'s unvalued block must not read as a list of holdings.

Live prod, 2026-08-19: the treasury holds $9.65 USDC plus four unsolicited
dust/scam tokens (an address-poisoning campaign). The agent read the portfolio
and summarised it as "$9.65 USDC + four ~$9 unvalued positions (DOS, badrp,
GOOK, NFLXB)" -- it called the bait "positions". The header said only "unvalued
-- deliberately excluded from the total", which explains the ARITHMETIC but not
what those rows ARE.

The prompt-side rule lives in the `treasury-trading` skill, but a tool result
that reads as a holdings list will keep inviting this. The warning belongs in
the output.
"""
import pytest

from tools.defi.data_tool import DefiDataTool, PortfolioParams


class _Identity:
    def __init__(self, symbol, decimals=18):
        self.symbol = symbol
        self.decimals = decimals


class _Price:
    def __init__(self, price_usd, confidence):
        self.price_usd = price_usd
        self.confidence = confidence


@pytest.fixture
def tool(monkeypatch):
    t = DefiDataTool()
    monkeypatch.setattr(t, "_resolve_holder", lambda: "0x" + "a" * 40)
    monkeypatch.setattr(
        t, "_index_fn",
        lambda holder, chain: {
            "0x" + "1" * 40: 9_650_000,          # USDC, priced
            "0x" + "2" * 40: 99 * 10 ** 18,      # dust, unpriceable
        }, raising=False)
    monkeypatch.setattr(t, "_identity", lambda chain, addr: _Identity(
        "USDC" if addr.endswith("1" * 4) else "GOOK",
        6 if addr.endswith("1" * 4) else 18))
    monkeypatch.setattr(t, "_price_for", lambda chain, addr: (
        _Price(1.0, "high") if addr.endswith("1" * 4) else _Price(None, "low")))
    return t


async def _portfolio(tool):
    res = await tool.portfolio(PortfolioParams(chain="base"))
    return res.extracted_content or res.long_term_memory or str(res)


@pytest.mark.asyncio
async def test_the_unvalued_block_says_these_are_not_your_holdings(tool):
    text = await _portfolio(tool)
    lowered = text.lower()
    assert "not necessarily holdings" in lowered or "did not buy" in lowered, text
    assert "dust" in lowered, "the output must name the failure mode it invites"


@pytest.mark.asyncio
async def test_the_unvalued_block_warns_against_trading_them(tool):
    text = (await _portfolio(tool)).lower()
    assert "never" in text and ("trade" in text or "sell" in text)


@pytest.mark.asyncio
async def test_the_valued_total_is_still_the_only_total(tool):
    text = await _portfolio(tool)
    assert "$9.65" in text
    assert "GOOK" in text, "dust is still REPORTED -- it is hidden from the total, not from the agent"
