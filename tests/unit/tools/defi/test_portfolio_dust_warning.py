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


# --------------------------------------------------------------------------
# A pricing outage must not turn your own treasury into "dust" (2026-08-25)
#
# Seen live: DexScreener returned no price for Base USDC for a few minutes, so
# the wallet's own registry-VERIFIED USDC fell into the unvalued block - whose
# header says "A token can sit at this address because someone SENT it to you...
# Treat anything here as noise to report, never as a position to realize." The
# agent read that and reported "USDC 11.91 sits in the UNVALUED dust block - not
# a position", about its entire spendable balance.
#
# Provenance and price are different questions. A canonical pin answers the
# first; a third-party indexer answers the second, and it is allowed to fail.
# --------------------------------------------------------------------------

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RANDOM_TOKEN = "0x1111111111111111111111111111111111111111"


def _unpriced_tool(**kw):
    from tools.defi.data_tool import DefiDataTool
    from tools.defi.providers.base import PriceInfo
    base = dict(
        holder="0x" + "22" * 20,
        balances_fn=lambda h, c, toks: {USDC_BASE: 11_910_340,
                                        RANDOM_TOKEN: 9_000_000_000},
        price_fn=lambda c, a: PriceInfo(price_usd=None, liquidity_usd=None,
                                        pool_count=0, confidence="unknown"),
        native_fn=lambda h, c: 0.001,
    )
    base.update(kw)
    return DefiDataTool(**base)


def _text(res):
    return res.extracted_content or ""


def _blocks(out: str):
    """(verified-but-unpriced block, dust block) — split on their own headers."""
    dust_at = out.index("unvalued — NOT necessarily holdings you bought")
    known_at = out.index("verified holdings whose PRICE is unavailable")
    if known_at < dust_at:
        return out[known_at:dust_at], out[dust_at:]
    return out[known_at:], out[dust_at:known_at]


@pytest.mark.asyncio
async def test_a_canonical_token_is_never_described_as_dust():
    from tools.defi.data_tool import PortfolioParams
    out = _text(await _unpriced_tool().portfolio(PortfolioParams(chain="base")))
    known, dust = _blocks(out)
    assert USDC_BASE in known, "verified USDC belongs with your own holdings"
    assert USDC_BASE not in dust, "and never under the 'someone SENT it' warning"


@pytest.mark.asyncio
async def test_the_canonical_balance_itself_is_still_reported():
    from tools.defi.data_tool import PortfolioParams
    out = _text(await _unpriced_tool().portfolio(PortfolioParams(chain="base")))
    assert "11.910340" in out and "USDC" in out


@pytest.mark.asyncio
async def test_an_unpriced_canonical_token_says_the_PRICE_is_missing():
    """Not the holding. The balance is known and on-chain; only its USD value
    is unavailable, and the two must not read the same."""
    from tools.defi.data_tool import PortfolioParams
    out = _text(await _unpriced_tool().portfolio(PortfolioParams(chain="base")))
    assert "price" in out.lower()
    assert "verified" in out.lower() or "canonical" in out.lower()


@pytest.mark.asyncio
async def test_an_unrecognised_token_still_gets_the_dust_warning():
    """The warning is load-bearing and must survive this change - it is what
    stops the agent selling an address-poisoning airdrop into a drainer."""
    from tools.defi.data_tool import PortfolioParams
    out = _text(await _unpriced_tool().portfolio(PortfolioParams(chain="base")))
    known, dust = _blocks(out)
    assert "SENT it to you" in dust
    assert RANDOM_TOKEN in dust
    assert RANDOM_TOKEN not in known


@pytest.mark.asyncio
async def test_no_price_is_still_no_value_in_the_total():
    """The fix is about FRAMING. An unpriceable token must never be counted."""
    from tools.defi.data_tool import PortfolioParams
    out = _text(await _unpriced_tool().portfolio(PortfolioParams(chain="base")))
    assert "$0.00" in out


@pytest.mark.asyncio
async def test_a_priced_canonical_token_still_lands_in_the_valued_block():
    from tools.defi.data_tool import PortfolioParams
    from tools.defi.providers.base import PriceInfo
    tool = _unpriced_tool(price_fn=lambda c, a: PriceInfo(
        price_usd=1.0, liquidity_usd=5e5, pool_count=28, confidence="high"))
    out = _text(await tool.portfolio(PortfolioParams(chain="base")))
    assert "$11.91" in out
