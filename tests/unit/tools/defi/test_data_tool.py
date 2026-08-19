"""defi_data — address-keyed sight, honest coverage, unknown never zero."""
import pytest

from tools.defi.data_tool import (
    CONTRACT_READ_GAS_CAP, CONTRACT_READ_MAX_BYTES, DefiDataTool,
    ContractReadParams, PortfolioParams, ResolveParams, TokenRefParams,
)
from tools.defi.providers.base import Candidate, PriceInfo, ScreenVerdict

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
FAKE = "0x1111111111111111111111111111111111111111"
SQUAT = "0xBAdBadbADBaDBADBadbADbaDBadBaDBADBadBAD0"


def _tool(**kw):
    return DefiDataTool(**kw)


def _cand(addr, liq, symbol="AERO"):
    return Candidate(chain="base", address=addr, symbol=symbol, name="Aerodrome",
                     liquidity_usd=liq, price_usd=1.0)


def _price(p=1.0, liq=1_000_000.0, pools=3, conf="high"):
    return PriceInfo(price_usd=p, liquidity_usd=liq, pool_count=pools, confidence=conf)


def _text(res):
    return res.extracted_content or ""


# --------------------------------------------------------------------------
# token_resolve — discovery aid, never a resolution
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_returns_candidates_never_a_pick():
    tool = _tool(search_fn=lambda s: [_cand(SQUAT, 99_000_000.0), _cand(USDC, 900.0)])
    out = _text(await tool.token_resolve(ResolveParams(symbol="AERO")))
    assert SQUAT in out and USDC in out, "every candidate must be shown"
    assert "must choose" in out.lower()


@pytest.mark.asyncio
async def test_resolve_single_match_is_still_a_candidate_list():
    tool = _tool(search_fn=lambda s: [_cand(USDC, 5.0)])
    out = _text(await tool.token_resolve(ResolveParams(symbol="ONLYONE")))
    assert "candidate" in out.lower()
    assert "must choose" in out.lower(), "one match today is not a safety property"


@pytest.mark.asyncio
async def test_resolve_states_which_chains_were_searched():
    tool = _tool(search_fn=lambda s: [_cand(USDC, 5.0)])
    out = _text(await tool.token_resolve(ResolveParams(symbol="X")))
    assert "base" in out.lower()


@pytest.mark.asyncio
async def test_resolve_no_matches_is_honest():
    tool = _tool(search_fn=lambda s: [])
    out = _text(await tool.token_resolve(ResolveParams(symbol="NOPE")))
    assert "no " in out.lower()


# --------------------------------------------------------------------------
# Address is identity
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_info_rejects_bad_checksum():
    tool = _tool()
    bad = "0x833589fcD6eDb6E08f4c7C32D4f71b54bdA02913"
    res = await tool.token_info(TokenRefParams(chain="base", address=bad))
    assert res.error and "checksum" in res.error.lower()


@pytest.mark.asyncio
async def test_token_info_rejects_a_symbol_as_address():
    tool = _tool()
    res = await tool.token_info(TokenRefParams(chain="base", address="AERO"))
    assert res.error, "a ticker is not an address"


@pytest.mark.asyncio
async def test_token_info_renders_address_and_verified_flag():
    tool = _tool(price_fn=lambda c, a: _price(), screen_fn=lambda c, a: ScreenVerdict(True, {"is_honeypot": "0"}, []))
    out = _text(await tool.token_info(TokenRefParams(chain="base", address=USDC)))
    assert USDC in out
    assert "verified" in out.lower()


@pytest.mark.asyncio
async def test_token_info_surfaces_screen_flags():
    tool = _tool(price_fn=lambda c, a: _price(),
                 screen_fn=lambda c, a: ScreenVerdict(True, {"is_honeypot": "1"}, ["honeypot"]))
    out = _text(await tool.token_info(TokenRefParams(chain="base", address=FAKE)))
    assert "honeypot" in out.lower()


@pytest.mark.asyncio
async def test_unavailable_screen_never_reads_as_safe():
    tool = _tool(price_fn=lambda c, a: _price(), screen_fn=lambda c, a: ScreenVerdict(False))
    out = _text(await tool.token_info(TokenRefParams(chain="base", address=FAKE)))
    assert "unavailable" in out.lower()
    assert "safe" not in out.lower()


# --------------------------------------------------------------------------
# Unknown is never zero
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_unknown_is_never_zero():
    tool = _tool(price_fn=lambda c, a: PriceInfo(None, None, 0, "unknown"))
    out = _text(await tool.price(TokenRefParams(chain="base", address=FAKE)))
    assert "unknown" in out.lower()
    assert "$0.00" not in out


@pytest.mark.asyncio
async def test_price_reports_confidence():
    tool = _tool(price_fn=lambda c, a: _price(conf="low", liq=900.0, pools=1))
    out = _text(await tool.price(TokenRefParams(chain="base", address=FAKE)))
    assert "low" in out.lower()


# --------------------------------------------------------------------------
# portfolio — coverage honesty and thin-pool exclusion
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_portfolio_labels_partial_coverage_without_a_key(monkeypatch):
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    tool = _tool(holder="0x" + "22" * 20,
                 balances_fn=lambda h, c, toks: {USDC: 10_000_000},
                 price_fn=lambda c, a: _price(p=1.0))
    out = _text(await tool.portfolio(PortfolioParams()))
    assert "partial" in out.lower()
    assert "scanned" in out.lower()


@pytest.mark.asyncio
async def test_portfolio_labels_indexed_coverage_with_a_key(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "k")
    tool = _tool(holder="0x" + "22" * 20,
                 index_fn=lambda h, chain="base": {USDC: 10_000_000},
                 price_fn=lambda c, a: _price(p=1.0))
    out = _text(await tool.portfolio(PortfolioParams()))
    assert "indexed" in out.lower()


@pytest.mark.asyncio
async def test_portfolio_excludes_low_confidence_from_the_total():
    """An attacker-seeded pool must not inflate the headline figure."""
    tool = _tool(holder="0x" + "22" * 20,
                 balances_fn=lambda h, c, toks: {USDC: 10_000_000, FAKE: 5_000_000_000_000_000_000},
                 price_fn=lambda c, a: (_price(p=1.0) if a == USDC
                                        else _price(p=1_000_000.0, liq=900.0, pools=1, conf="low")))
    out = _text(await tool.portfolio(PortfolioParams()))
    assert "unvalued" in out.lower()
    assert "5,000,000,000,000" not in out.replace(" ", "")


@pytest.mark.asyncio
async def test_portfolio_unknown_balance_is_not_zero():
    tool = _tool(holder="0x" + "22" * 20,
                 balances_fn=lambda h, c, toks: {USDC: None},
                 price_fn=lambda c, a: _price(p=1.0))
    out = _text(await tool.portfolio(PortfolioParams()))
    assert "unknown" in out.lower()


@pytest.mark.asyncio
async def test_portfolio_without_a_wallet_is_honest():
    tool = _tool(holder=None)
    res = await tool.portfolio(PortfolioParams())
    assert res.error and "wallet" in res.error.lower()


# --------------------------------------------------------------------------
# contract_read — raw always, decode labelled, bounded
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contract_read_returns_raw_hex_and_labels_the_decode():
    tool = _tool(call_fn=lambda **k: "0x" + "00" * 31 + "2a")
    out = _text(await tool.contract_read(
        ContractReadParams(chain="base", address=FAKE, signature="totalSupply()", args=[])))
    assert "0x" in out
    assert "raw" in out.lower()
    assert "best-effort" in out.lower()


@pytest.mark.asyncio
async def test_contract_read_truncates_explicitly():
    big = "0x" + "ab" * (CONTRACT_READ_MAX_BYTES + 100)
    tool = _tool(call_fn=lambda **k: big)
    out = _text(await tool.contract_read(
        ContractReadParams(chain="base", address=FAKE, signature="data()", args=[])))
    assert "truncated" in out.lower()


@pytest.mark.asyncio
async def test_contract_read_rejects_bad_address():
    tool = _tool()
    res = await tool.contract_read(
        ContractReadParams(chain="base", address="0xnope", signature="x()", args=[]))
    assert res.error


def test_caps_are_bounded():
    assert 0 < CONTRACT_READ_GAS_CAP <= 5_000_000
    assert 0 < CONTRACT_READ_MAX_BYTES <= 65536


# --------------------------------------------------------------------------
# multi-chain data tier (2026-08-17)
# --------------------------------------------------------------------------

def test_the_data_tier_covers_every_chain_the_registry_can_read():
    """The tier said "base only" while the registry knew five chains. Reading
    is safe on all of them — it is MOVING value that needs verification."""
    from core.wallet import chains
    from tools.defi.data_tool import SUPPORTED_CHAINS
    assert set(SUPPORTED_CHAINS) == {r.name for r in chains.all_rows()}
    assert "ethereum" in SUPPORTED_CHAINS
    assert "robinhood" in SUPPORTED_CHAINS


def test_the_data_tier_still_refuses_a_chain_it_does_not_know():
    from tools.defi.data_tool import DefiDataTool
    addr, err = DefiDataTool()._validate("nosuchchain", "0x" + "11" * 20)
    assert addr is None
    assert "nosuchchain" in err


@pytest.mark.asyncio
async def test_portfolio_reports_the_chain_it_was_asked_for(monkeypatch):
    """portfolio hardcoded base, so an ethereum holding was invisible with no
    hint that it had not been looked at."""
    from tools.defi.data_tool import PortfolioParams
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    seen = {}

    def _balances(holder, chain, toks):
        seen["chain"] = chain
        return {}

    tool = _tool(holder="0x" + "22" * 20, balances_fn=_balances,
                 price_fn=lambda c, a: _price(p=1.0))
    out = _text(await tool.portfolio(PortfolioParams(chain="ethereum")))
    assert seen["chain"] == "ethereum"
    assert "ethereum" in out


@pytest.mark.asyncio
async def test_portfolio_indexes_the_requested_chain(monkeypatch):
    """The indexer must be asked for the SAME chain, or it enumerates another
    network's holdings and labels them this one's."""
    from tools.defi.data_tool import PortfolioParams
    monkeypatch.setenv("ALCHEMY_API_KEY", "k")
    seen = {}

    def _index(holder, chain="base"):
        seen["chain"] = chain
        return {USDC: 10_000_000}

    tool = _tool(holder="0x" + "22" * 20, index_fn=_index,
                 price_fn=lambda c, a: _price(p=1.0))
    await tool.portfolio(PortfolioParams(chain="ethereum"))
    assert seen["chain"] == "ethereum"
