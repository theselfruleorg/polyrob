"""`token_holders` — concentration, and `token_info` saying when a screen is partial."""
import pytest

from tools.defi.data_tool import DefiDataTool, TokenRefParams
from tools.defi.providers.base import (
    HolderReport, HolderRow, PriceInfo, ScreenVerdict,
)

TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _text(res):
    return (res.extracted_content or "") + (res.error or "")


def _price(**kw):
    d = dict(price_usd=1.0, liquidity_usd=1_000_000.0, pool_count=3, confidence="high")
    d.update(kw)
    return PriceInfo(**d)


def _ident(chain, addr):
    import types
    return types.SimpleNamespace(symbol="TKN", name="Token", decimals=18,
                                 verified=True, metadata_changed=False)


def _report(**kw):
    d = dict(
        available=True, holder_count=35236, total_supply=1_000_000_000.0,
        top_holders=[
            HolderRow("0xpool", percent=0.30, is_contract=True, is_locked=False, tag="UniswapV3"),
            HolderRow("0xwhale", percent=0.22, is_contract=False, is_locked=False),
            HolderRow("0xsmall", percent=0.01, is_contract=False, is_locked=False),
        ],
        lp_holders=[HolderRow("0xdead", percent=0.999, is_contract=False, is_locked=True)],
        lp_holder_count=4, lp_total_supply=26.18,
        creator_address="0xcreator", creator_percent=0.05,
        owner_address="0xowner", owner_percent=0.0,
        honeypot_with_same_creator=False,
    )
    d.update(kw)
    return HolderReport(**d)


def _tool(**kw):
    kw.setdefault("identity_fn", _ident)
    kw.setdefault("price_fn", lambda c, a: _price())
    return DefiDataTool(**kw)


@pytest.mark.asyncio
async def test_token_holders_shows_every_top_row_with_its_percent():
    tool = _tool(holders_fn=lambda c, a: _report())
    out = _text(await tool.token_holders(TokenRefParams(chain="base", address=TOKEN)))
    assert "0xwhale" in out and "22" in out
    assert "0xpool" in out


@pytest.mark.asyncio
async def test_token_holders_separates_contracts_from_wallets():
    tool = _tool(holders_fn=lambda c, a: _report())
    out = _text(await tool.token_holders(TokenRefParams(chain="base", address=TOKEN)))
    assert "contract" in out.lower()


@pytest.mark.asyncio
async def test_token_holders_reports_lp_lock_state():
    tool = _tool(holders_fn=lambda c, a: _report())
    out = _text(await tool.token_holders(TokenRefParams(chain="base", address=TOKEN)))
    assert "lp" in out.lower() and "locked" in out.lower()


@pytest.mark.asyncio
async def test_token_holders_names_the_creator_stake():
    tool = _tool(holders_fn=lambda c, a: _report())
    out = _text(await tool.token_holders(TokenRefParams(chain="base", address=TOKEN)))
    assert "0xcreator" in out


@pytest.mark.asyncio
async def test_a_serial_scammer_creator_is_called_out():
    tool = _tool(holders_fn=lambda c, a: _report(honeypot_with_same_creator=True))
    out = _text(await tool.token_holders(TokenRefParams(chain="base", address=TOKEN)))
    assert "same creator" in out.lower() or "honeypot" in out.lower()


@pytest.mark.asyncio
async def test_unavailable_report_states_its_reason_and_is_not_a_clean_result():
    tool = _tool(holders_fn=lambda c, a: HolderReport(
        available=False, reason="this chain's screener sent no holder data"))
    out = _text(await tool.token_holders(TokenRefParams(chain="robinhood", address=TOKEN)))
    assert "sent no holder data" in out
    assert "not" in out.lower(), "unavailable must never read as well-distributed"


@pytest.mark.asyncio
async def test_holders_never_renders_an_unknown_as_zero():
    """A figure the screener did not send is `unknown`, never 0.

    An unreported holder count rendered as 0 reads as "nobody holds this",
    and an unreported share as "0%" reads as "this wallet is empty".
    """
    tool = _tool(holders_fn=lambda c, a: _report(
        holder_count=None, total_supply=None,
        top_holders=[HolderRow("0xw", percent=None, is_contract=None)],
        lp_holders=[], lp_holder_count=None, lp_total_supply=None,
        creator_address=None, owner_address=None,
        honeypot_with_same_creator=None))
    out = _text(await tool.token_holders(TokenRefParams(chain="base", address=TOKEN)))
    assert "holder count: unknown" in out
    assert "0xw  unknown  wallet-or-contract unknown" in out
    assert "0%" not in out, "no unknown figure may render as a zero"


@pytest.mark.asyncio
async def test_bad_address_is_refused_before_any_provider_call():
    called = []
    tool = _tool(holders_fn=lambda c, a: called.append(1) or _report())
    res = await tool.token_holders(TokenRefParams(chain="base", address="not-an-address"))
    assert res.error
    assert not called


# --- D5: token_info carries a concentration line --------------------------

@pytest.mark.asyncio
async def test_token_info_shows_top_holder_concentration():
    tool = _tool(screen_fn=lambda c, a: ScreenVerdict(available=True, checks={"is_honeypot": "0"}),
                 holders_fn=lambda c, a: _report())
    out = _text(await tool.token_info(TokenRefParams(chain="base", address=TOKEN)))
    assert "holders" in out.lower()
    assert "53" in out, "top-3 stake 30+22+1 = 53% must be stated"


# --- D6: a partial screen must not read as a clean screen -----------------

@pytest.mark.asyncio
async def test_token_info_says_partial_when_checks_did_not_run():
    tool = _tool(
        screen_fn=lambda c, a: ScreenVerdict(
            available=True, checks={"cannot_buy": "0"}, flags=[],
            missing=["is_honeypot", "is_mintable", "buy_tax"]),
        holders_fn=lambda c, a: HolderReport(available=False, reason="none sent"))
    out = _text(await tool.token_info(TokenRefParams(chain="robinhood", address=TOKEN)))
    assert "PARTIAL" in out
    assert "is_honeypot" in out
    # An unqualified "no risk flags raised" over a payload that never carried
    # is_honeypot is the sentence that makes a partial screen read as clean.
    assert "no risk flags raised among the checks THAT RAN" in out
    assert "did NOT run" in out


@pytest.mark.asyncio
async def test_token_info_still_says_clean_when_every_check_ran():
    tool = _tool(
        screen_fn=lambda c, a: ScreenVerdict(
            available=True, checks={"is_honeypot": "0"}, flags=[], missing=[]),
        holders_fn=lambda c, a: _report())
    out = _text(await tool.token_info(TokenRefParams(chain="base", address=TOKEN)))
    assert "no risk flags raised" in out
    assert "PARTIAL" not in out
