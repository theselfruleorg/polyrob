"""085 EV-2 (Solana): an unpriced sell of an untrusted mint into USDC above the
$5 ticket goes to the OWNER (D5), not a plain refusal; the owner confirming it
lets it through, and a trusted mint (own launch) keeps the lift."""
import types

import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from core.wallet import token_pins
from core.wallet import token_provenance as tp
from tools.defi import buy_screen
from tools.defi.trade_tool import DefiTradeTool, SolanaSwapParams

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
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
    monkeypatch.setattr(buy_screen, "owner_confirms", lambda *a, **k: False)
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


def _quote(ti, to, amt):
    from tools.defi.providers.jupiter import JupiterQuote
    out = 40 * 10 ** 6
    floor = out * 99 // 100
    return JupiterQuote(chain="solana", token_in=ti, token_out=to,
                        amount_in_raw=amt, amount_out_raw=out,
                        amount_out_min_raw=floor, venue="jupiter:Orca",
                        raw={"outAmount": str(out), "otherAmountThreshold": str(floor)})


def _build(*a, **k):
    raise RuntimeError(BUILD_REACHED)


def _tool():
    return DefiTradeTool(
        wallet=_Wallet(),
        price_fn=lambda c, a: 1.0 if a == USDC else None,
        fallback_price_fn=lambda c, a: None,
        solana_quote_fn=lambda ti, to, amt, **k: _quote(ti, to, amt),
        solana_build_fn=_build,
        solana_decimals_fn=lambda mint: 6)


async def _sell():
    ctx = types.SimpleNamespace(user_id="rob", metadata={}, role="orchestrator",
                                is_sub_agent=False)
    try:
        res = await _tool().solana_swap(
            SolanaSwapParams(token_in=OTHER, token_out=USDC, amount_in=400.0,
                             max_spend_usd=45.0, dry_run=False),
            execution_context=ctx)
    except RuntimeError as exc:
        assert str(exc) == BUILD_REACHED
        return None
    return res.error


@pytest.mark.asyncio
async def test_an_untrusted_unpriced_sell_goes_to_the_owner():
    err = await _sell()
    assert err and "owner approval required" in err and "owner_queue" in err


@pytest.mark.asyncio
async def test_the_owner_confirming_lets_it_through(monkeypatch):
    monkeypatch.setattr(buy_screen, "owner_confirms", lambda *a, **k: True)
    assert await _sell() is None


@pytest.mark.asyncio
async def test_a_trusted_mint_sold_keeps_the_lift():
    tp.record_own_token("solana", OTHER, kind="launchpad_launch", evidence="tx")
    assert await _sell() is None
