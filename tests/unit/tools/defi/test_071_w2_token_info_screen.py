"""071 W2 — token_info renders ONE merged screen; token_holders reads Solana
holders from the chain; ohlcv names the indexer's pick next to a caller pool."""
import types

import pytest

from tools.defi import token_screen as ts
from tools.defi.data_tool import DefiDataTool, OhlcvParams, TokenRefParams
from tools.defi.providers.base import PriceInfo, ScreenVerdict

MINT = "Hu6xf6bNiJ5ErRaG7Yhos89jgfAdCG1tu8DCips2pump"
TOKEN = "0x1111111111111111111111111111111111111111"


def _ident():
    return types.SimpleNamespace(symbol="X", name="X", decimals=6, verified=False,
                                 metadata_changed=False)


def _price():
    return PriceInfo(price_usd=1.0, liquidity_usd=9e6, pool_count=9, confidence="high")


def _text(res):
    return res.extracted_content or ""


def _tool(**kw):
    kw.setdefault("price_fn", lambda c, a: _price())
    kw.setdefault("identity_fn", lambda c, a: _ident())
    return DefiDataTool(**kw)


@pytest.mark.asyncio
async def test_rpc_hard_fail_shows_even_when_goplus_is_clean():
    from core.wallet.spl_facts import MintFacts
    facts = MintFacts(mint=MINT, program="spl-token-2022", decimals=6, supply_raw=10 ** 15,
                      mint_authority=None, freeze_authority=None,
                      extensions={"permanentDelegate": {"delegate": "DELEGATE"}})
    tool = _tool(screen_fn=lambda c, a: ScreenVerdict(True, {"mintable": "no"}, [], []),
                 facts_fn=lambda c, a: [ts.from_svm_mint(facts)])
    res = await tool.token_info(TokenRefParams(chain="solana", address=MINT))
    out = _text(res)
    assert "HARD FAIL" in out and "permanent_delegate" in out
    # The prose names every source once; the per-check table is metadata.
    assert "permanent_delegate (rpc)" in out and "goplus" in out
    assert {"goplus", "rpc"} <= {c["source"] for c in res.metadata["screen"]["checks"]}
    # The typed metadata carries the SAME merged screen the prose renders.
    assert res.metadata["screen"]["hard_fails"] == ["permanent_delegate (rpc)"]
    assert "rpc" in res.metadata["screen"]["sources"]


@pytest.mark.asyncio
async def test_every_other_source_down_makes_the_screen_partial_not_clean():
    # Default facts: the conftest blocks RPC and providers -> all NOT CHECKED.
    tool = _tool(screen_fn=lambda c, a: ScreenVerdict(True, {"is_honeypot": "0"}, [], []),
                 holders_fn=lambda c, a: None)
    out = _text(await tool.token_info(TokenRefParams(chain="base", address=TOKEN)))
    assert "PARTIAL" in out and "not run:" in out
    assert "no risk flags among the checks that ran" in out
    assert "owner/proxy" in out and "a check that did not run is not a pass" in out


@pytest.mark.asyncio
async def test_a_raising_facts_seam_never_reads_as_clean():
    def boom(c, a):
        raise RuntimeError("x")
    tool = _tool(screen_fn=lambda c, a: ScreenVerdict(False), facts_fn=boom,
                 holders_fn=lambda c, a: None)
    out = _text(await tool.token_info(TokenRefParams(chain="base", address=TOKEN)))
    assert "UNSCREENED" in out


@pytest.mark.asyncio
async def test_token_info_on_solana_uses_the_screens_holder_rows():
    from tools.defi.providers.base import HolderReport, HolderRow
    rep = ts.SourceReport("rpc-holders", checks=[ts.Check("holder_concentration", "top-1 60%", "rpc")])
    rep.holders = HolderReport(available=True, top_holders=[HolderRow("OWNER", 0.6, True)])
    tool = _tool(screen_fn=lambda c, a: ScreenVerdict(False), facts_fn=lambda c, a: [rep])
    out = _text(await tool.token_info(TokenRefParams(chain="solana", address=MINT)))
    assert "holders:  unknown addresses; top 1 hold 60.0%" in out


@pytest.mark.asyncio
async def test_token_holders_on_solana_reads_the_chain(monkeypatch):
    from core.wallet import spl_facts
    from core.wallet.spl_facts import HolderLine, MintFacts
    monkeypatch.setattr(spl_facts, "read_mint", lambda m, **k: MintFacts(
        mint=m, program="spl-token", decimals=6, supply_raw=10 ** 9,
        mint_authority=None, freeze_authority=None))
    monkeypatch.setattr(spl_facts, "read_holders", lambda m, s, **k: [
        HolderLine("TA1", "CURVEPDA", 6 * 10 ** 8, 0.6, True, "pump.fun bonding curve"),
        HolderLine("TA2", "WALLET1", 10 ** 8, 0.1, False)])
    out = _text(await DefiDataTool().token_holders(TokenRefParams(chain="solana", address=MINT)))
    assert "CURVEPDA" in out and "pump.fun bonding curve" in out
    assert "60.0%" in out and "UNAVAILABLE" not in out


@pytest.mark.asyncio
async def test_token_holders_on_solana_fails_closed_with_the_reason():
    out = _text(await DefiDataTool().token_holders(TokenRefParams(chain="solana", address=MINT)))
    assert "UNAVAILABLE" in out and "DEFI_SOLANA_RPC" in out


@pytest.mark.asyncio
async def test_ohlcv_names_the_indexer_pick_next_to_a_caller_pool():
    from tools.defi.providers.geckoterminal import Candle, PoolPick
    deep = "0x" + "d" * 40
    thin = "0x" + "e" * 40
    tool = DefiDataTool(
        pool_for_token_fn=lambda c, a: PoolPick(deep, 18350.0, 3, volume_h24_usd=61792.0),
        ohlcv_fn=lambda c, p, **kw: [Candle(1_700_000_000, 1.0, 1.0, 1.0, 1.0)])
    out = _text(await tool.ohlcv(OhlcvParams(chain="base", address=TOKEN, pool=thin)))
    assert "NOT the indexer's pick" in out and deep in out and "$18,350" in out
