"""Solana Phase 1 — the READ tier is wider than the money tier (029 §5).

Three of our providers already index Solana, so refusing to look was a blind
spot rather than caution. What must NOT happen is the read tier implying a money
tier that does not exist: there is no Solana signer, no broadcast rail and no
transaction simulation, so every money verb must refuse Solana BY NAME, and the
read verbs must say plainly what they cannot promise (base58 has no checksum).
"""
import pytest

from tools.defi.data_tool import DefiDataTool, PortfolioParams, TokenRefParams

SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
EVM_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _text(res):
    return res.extracted_content or ""


def _ident(symbol="USDC", name="USD Coin", decimals=6):
    return type("I", (), {"symbol": symbol, "name": name, "decimals": decimals,
                          "verified": False, "metadata_changed": False})()


# -- the money tier stays shut ----------------------------------------------

def test_solana_is_not_money_capable():
    from core.wallet import chains
    ok, why = chains.money_capable("solana")
    assert not ok
    assert "solana" in why.lower()


def test_solana_has_no_swap_route():
    from core.wallet import chains
    ok, why = chains.swap_ready("solana")
    assert not ok


def test_solana_is_not_in_the_money_or_swap_chain_lists():
    from core.wallet import chains
    assert "solana" not in chains.money_chains()
    assert "solana" not in chains.swap_chains()


@pytest.mark.asyncio
async def test_every_money_verb_refuses_solana_by_name():
    from tools.defi.trade_tool import (ApproveParams, DefiTradeTool,
                                       SwapParams, TransferParams)
    tool = DefiTradeTool()
    calls = [
        tool.transfer(TransferParams(chain="solana", token=SOL_USDC,
                                     to=SOL_USDC, amount=1.0, max_spend_usd=1.0)),
        tool.approve_token(ApproveParams(chain="solana", token=SOL_USDC,
                                         spender=SOL_USDC, amount=1.0,
                                         max_spend_usd=1.0)),
        tool.swap(SwapParams(chain="solana", token_in=SOL_USDC,
                             token_out=SOL_USDC, amount_in=1.0, max_spend_usd=1.0)),
    ]
    for coro in calls:
        res = await coro
        assert res.error and "solana" in res.error.lower()


def test_solana_can_never_be_signed_through_the_evm_rail():
    """chain_id 0 is falsey and signer.sign_transaction refuses a missing
    chainId, so the EVM signing perimeter closes even if a caller got there."""
    from core.wallet import chains
    assert chains.get("solana").chain_id == 0


def test_solana_is_excluded_from_the_evm_rpc_read_table():
    from core.wallet import chains
    assert "solana" not in [r.name for r in chains.evm_rows()]


# -- the read tier is open ---------------------------------------------------

@pytest.mark.asyncio
async def test_token_info_accepts_a_base58_mint():
    from tools.defi.providers.base import PriceInfo, ScreenVerdict
    tool = DefiDataTool(
        price_fn=lambda c, a: PriceInfo(price_usd=1.0, liquidity_usd=9e6,
                                        pool_count=9, confidence="high"),
        screen_fn=lambda c, a: ScreenVerdict(available=True,
                                             checks={"freezable": "yes"},
                                             flags=["freezable"]),
        identity_fn=lambda c, a: _ident())
    out = _text(await tool.token_info(TokenRefParams(chain="solana", address=SOL_USDC)))
    assert SOL_USDC in out
    assert "freezable" in out


@pytest.mark.asyncio
async def test_token_info_on_solana_says_base58_has_no_checksum():
    """The EVM habit ('a wrong address would have failed the checksum') is
    simply false here, and carrying it across is how funds go somewhere real."""
    from tools.defi.providers.base import PriceInfo, ScreenVerdict
    tool = DefiDataTool(
        price_fn=lambda c, a: PriceInfo(price_usd=1.0, liquidity_usd=9e6,
                                        pool_count=9, confidence="high"),
        screen_fn=lambda c, a: ScreenVerdict(available=True, checks={}, flags=[]),
        identity_fn=lambda c, a: _ident())
    out = _text(await tool.token_info(TokenRefParams(chain="solana", address=SOL_USDC)))
    assert "checksum" in out.lower()


@pytest.mark.asyncio
async def test_an_evm_address_is_refused_on_solana():
    tool = DefiDataTool()
    res = await tool.token_info(TokenRefParams(chain="solana", address=EVM_USDC))
    assert res.error


@pytest.mark.asyncio
async def test_a_base58_mint_is_refused_on_an_evm_chain():
    tool = DefiDataTool()
    res = await tool.token_info(TokenRefParams(chain="base", address=SOL_USDC))
    assert res.error


@pytest.mark.asyncio
async def test_portfolio_on_solana_reports_holdings(monkeypatch):
    """Phase 2: there IS a Solana address now, so portfolio reads rather than
    refuses. Coverage is COMPLETE without an indexer key — the parity win."""
    from core.wallet import solana_onchain
    monkeypatch.setattr(solana_onchain, "native_balance", lambda a, **k: 0.5)
    monkeypatch.setattr(solana_onchain, "token_balances",
                        lambda a, **k: {SOL_USDC: 11_910_340})
    from tools.defi.providers.base import PriceInfo
    tool = DefiDataTool(
        holder="HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk",
        price_fn=lambda c, a: PriceInfo(price_usd=1.0, liquidity_usd=9e6,
                                        pool_count=22, confidence="high"),
        identity_fn=lambda c, a: _ident())
    out = _text(await tool.portfolio(PortfolioParams(chain="solana")))
    assert "gas (SOL): 0.500000000 SOL" in out
    assert "$11.91" in out
    assert "complete" in out.lower()


@pytest.mark.asyncio
async def test_portfolio_on_solana_names_the_guarded_sell_path():
    """The read tier must not imply a money tier: selling is a different verb
    behind a different flag, and the note must say so honestly (2026-08-27 —
    the old "no Solana money rail" claim went stale when solana_swap shipped)."""
    from core.wallet import solana_onchain
    tool = DefiDataTool(holder="HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk")
    out = _text(await tool.portfolio(PortfolioParams(chain="solana")))
    assert "solana_swap" in out and "SOLANA_TRADE_ENABLED" in out


@pytest.mark.asyncio
async def test_a_failed_solana_token_read_is_not_an_empty_wallet(monkeypatch):
    from core.wallet import solana_onchain
    monkeypatch.setattr(solana_onchain, "token_balances", lambda a, **k: None)
    monkeypatch.setattr(solana_onchain, "native_balance", lambda a, **k: 1.0)
    tool = DefiDataTool(holder="HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk")
    out = _text(await tool.portfolio(PortfolioParams(chain="solana")))
    assert "UNAVAILABLE" in out
    assert "NOT" in out


@pytest.mark.asyncio
async def test_no_sol_says_what_it_blocks(monkeypatch):
    from core.wallet import solana_onchain
    monkeypatch.setattr(solana_onchain, "native_balance", lambda a, **k: 0.0)
    monkeypatch.setattr(solana_onchain, "token_balances", lambda a, **k: {})
    tool = DefiDataTool(holder="HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk")
    out = _text(await tool.portfolio(PortfolioParams(chain="solana")))
    assert "rent" in out.lower() and "fee" in out.lower()
