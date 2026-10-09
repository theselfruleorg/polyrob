"""A TRUSTED Solana buy is route-checked against the exit-grade price (DEFI-6).

The EVM swap got this in 83d80b535; ``solana_swap`` still held an owner pin,
an own launch or the owner's target to the $5 unchecked-route ticket because
such a mint has no spend-grade price. Now a trusted buy is checked against the
liquidity-backed exit-grade price: a fair Jupiter quote AGREES and passes, a
100x worse quote DISAGREES and refuses, and an untrusted mint keeps the ticket.
"""
import types

import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from core.wallet import token_pins
from core.wallet import token_provenance as tp
from tools.defi.trade_tool import DefiTradeTool, SolanaSwapParams

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
ME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
OURS = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
OTHER = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
BUILD_REACHED = "BUILD_REACHED"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setattr("core.wallet.solana_onchain._rpc",
                        lambda m, p: {"value": {"decimals": 6}})
    tp._reset_for_tests()
    monkeypatch.setattr(tp, "_PROBES", {})
    pins = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: pins)
    monkeypatch.setattr(tp, "provenance_db_path",
                        lambda data_home=None: str(tmp_path / "prov.db"))
    tp.record_own_token("solana", OURS, kind="launchpad_launch", evidence="tx")
    yield
    tp._reset_for_tests()


class _Wallet:
    def __init__(self):
        self.policy = types.SimpleNamespace(has_daily_cap=True)

    def solana_signer(self, account=0):
        return types.SimpleNamespace(address=ME, sign_transaction=lambda tx: tx)

    @property
    def solana_address(self):
        return ME


def _quote(ti, to, amt, *, factor=1.0):
    from tools.defi.providers.jupiter import JupiterQuote
    # 40 USDC at $0.10 a token -> 400 tokens (6 decimals)
    out = int(400 * factor * 10 ** 6)
    floor = out * 99 // 100
    return JupiterQuote(chain="solana", token_in=ti, token_out=to,
                        amount_in_raw=amt, amount_out_raw=out,
                        amount_out_min_raw=floor, venue="jupiter:Orca",
                        raw={"outAmount": str(out), "otherAmountThreshold": str(floor)})


def _build(*a, **k):
    raise RuntimeError(BUILD_REACHED)


def _tool(factor=1.0):
    from tools.defi.providers.base import ScreenVerdict
    return DefiTradeTool(
        wallet=_Wallet(),
        price_fn=lambda c, a: 1.0 if a == USDC else None,
        fallback_price_fn=lambda c, a: 0.10 if a in (OURS, OTHER) else None,
        solana_quote_fn=lambda ti, to, amt, **k: _quote(ti, to, amt, factor=factor),
        solana_build_fn=_build,
        solana_decimals_fn=lambda mint: 6,
        solana_screen_fn=lambda m: ScreenVerdict(available=True,
                                                 checks={"transfer_fee": "no"}))


def _ctx():
    return types.SimpleNamespace(user_id="rob", metadata={}, role="orchestrator",
                                 is_sub_agent=False)


async def _swap(tool, token_out):
    try:
        res = await tool.solana_swap(
            SolanaSwapParams(token_in=USDC, token_out=token_out, amount_in=40.0,
                             max_spend_usd=45.0, dry_run=False),
            execution_context=_ctx())
    except RuntimeError as exc:
        assert str(exc) == BUILD_REACHED
        return None
    return res.error


@pytest.mark.asyncio
async def test_a_trusted_buy_with_a_fair_quote_is_not_ticket_capped():
    assert await _swap(_tool(), OURS) is None


@pytest.mark.asyncio
async def test_a_100x_worse_quote_for_a_trusted_buy_still_refuses():
    err = await _swap(_tool(factor=0.01), OURS)
    assert err and "DISAGREES" in err


@pytest.mark.asyncio
async def test_an_untrusted_mint_keeps_the_unchecked_ticket():
    err = await _swap(_tool(), OTHER)
    assert err and "$5.00" in err


@pytest.mark.asyncio
async def test_an_owner_pinned_mint_is_trusted_too():
    token_pins.pin("solana", OTHER, "OTHER")
    assert await _swap(_tool(), OTHER) is None
