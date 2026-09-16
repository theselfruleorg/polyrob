"""Verb-side halves of S3/S4 (2026-09-14).

``approve_token``'s own USD pre-check sat INSIDE ``if unit_price:``, so a token
no source can price carried no bound at all before the guard saw it — and the
guard's exit exemption then valued it at $0.00. ``defi_trade.call`` had no
unlimited-approval floor at all, so the one grant shape ``approve_token``
refuses outright (2**128 and up) was reachable through the generic verb.

Both are pinned here. ``revoke_approval`` declares no grant and must stay
reachable for any token at any time — cleaning up a worthless or unpriceable
allowance is the hygiene the whole design wants easy.

Also M7: ``_fallback_price`` — the pricer the exit exemption consults — took
DexScreener's number with no trust signal at all, while ``_price`` next door
reads the same ``PriceInfo.confidence`` the provider already computes. The
fallback is deliberately WEAKER than the high-confidence bar (that is its whole
job), but "weaker" is not "unconditional": a price with no measured depth
behind it is a number, not a valuation.
"""
import contextlib

import pytest

from core.wallet.tx_guard import Decision
from tools.defi.trade_tool import (ApproveParams, CallParams, DefiTradeTool,
                                   RevokeParams)

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
#: Base's pinned Uniswap V3 SwapRouter02 — the spender a real exit approves.
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
ATTACKER = "0x1111111111111111111111111111111111111111"


class _Gate:
    def __init__(self):
        self.recorded = []

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def record(self, **kw):
        self.recorded.append(kw)


class _Signer:
    address = "0x2222222222222222222222222222222222222222"


class _Wallet:
    def __init__(self, gate):
        self.policy = gate

    def operational_signer(self):
        return _Signer()


class _Rail:
    last = None

    def __init__(self, chain, signer, **kw):
        self.sent = False
        _Rail.last = self

    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value}


def _tool(*, price=None, captured=None):
    gate = _Gate()

    def _guard(intent, tx, **kw):
        if captured is not None:
            captured.append(intent)
        return Decision(allowed=True, reason="test", lane="autonomous",
                        amount_usd=1.0)

    return DefiTradeTool(
        wallet=_Wallet(gate), rail_factory=_Rail, guard_fn=_guard,
        price_fn=lambda c, a: price,
        fallback_price_fn=lambda c, a: None,
        balance_fn=lambda c, h, t: 10 ** 12,
    ), gate


# --------------------------------------------------------------------------
# approve_token — an unvalued grant must not slip past the pre-check
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unpriceable_grant_to_an_arbitrary_spender_refuses():
    """The S3 attack shape, refused at the verb with a remedy instead of
    reaching the guard as a $0.00 authorization."""
    tool, _ = _tool(price=None)
    res = await tool.approve_token(ApproveParams(
        token=USDC, spender=ATTACKER, amount=1.0, max_spend_usd=5.0,
        dry_run=True))
    assert res.error and "price" in res.error.lower()
    assert ATTACKER.lower() in res.error.lower()


@pytest.mark.asyncio
async def test_an_unpriceable_grant_to_the_pinned_router_still_reaches_the_guard():
    """The sell leg of an unpriceable position stays reachable — the guard
    charges it the autonomous ceiling and asks the owner."""
    captured = []
    tool, _ = _tool(price=None, captured=captured)
    res = await tool.approve_token(ApproveParams(
        token=USDC, spender=ROUTER, amount=1.0, max_spend_usd=5.0,
        dry_run=True))
    assert res.error is None, res.error
    assert captured, "the guard was never consulted"


@pytest.mark.asyncio
async def test_a_priced_grant_to_an_arbitrary_spender_is_unchanged():
    """The spender rule rides on UNPRICEABILITY. A priced token is bounded by
    the USD pre-check exactly as before, whoever the spender is."""
    captured = []
    tool, _ = _tool(price=1.0, captured=captured)
    res = await tool.approve_token(ApproveParams(
        token=USDC, spender=ATTACKER, amount=1.0, max_spend_usd=5.0,
        dry_run=True))
    assert res.error is None, res.error
    assert captured


@pytest.mark.asyncio
async def test_a_revoke_of_an_unpriceable_token_is_never_blocked():
    captured = []
    tool, _ = _tool(price=None, captured=captured)
    res = await tool.revoke_approval(RevokeParams(
        token=USDC, spender=ATTACKER, dry_run=True))
    assert res.error is None, res.error
    assert captured and captured[0].expected_allowance_grants == ()


# --------------------------------------------------------------------------
# defi_trade.call — the unlimited-approval floor applies here too (S4)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_call_declaring_an_effectively_unlimited_grant_refuses(monkeypatch):
    monkeypatch.setenv("DEFI_CALL_ENABLED", "true")
    tool, _ = _tool(price=1.0)
    res = await tool.call(CallParams(
        chain="base", to=ROUTER, calldata="0xdeadbeef", spend_token=USDC,
        spend_max_raw=1, allow_spender=ROUTER, allow_max_raw=2 ** 128,
        max_spend_usd=5.0))
    assert res.error and "UNLIMITED" in res.error


@pytest.mark.asyncio
async def test_a_call_declaring_a_bounded_grant_still_builds(monkeypatch):
    monkeypatch.setenv("DEFI_CALL_ENABLED", "true")
    captured = []
    tool, _ = _tool(price=1.0, captured=captured)
    res = await tool.call(CallParams(
        chain="base", to=ROUTER, calldata="0xdeadbeef", spend_token=USDC,
        spend_max_raw=1, allow_spender=ROUTER, allow_max_raw=1_000_000,
        max_spend_usd=5.0))
    assert res.error is None, res.error
    assert captured[0].expected_allowance_grants[0][2] == 1_000_000


# --------------------------------------------------------------------------
# M7 — the fallback pricer carries a trust signal
# --------------------------------------------------------------------------

def _bare_tool():
    """No injected price seams, so the real `_price`/`_fallback_price` bodies
    run against a stubbed provider."""
    return DefiTradeTool(wallet=_Wallet(_Gate()), rail_factory=_Rail)


def _info(price, liquidity, pools, confidence):
    from tools.defi.providers.base import PriceInfo
    return PriceInfo(price_usd=price, liquidity_usd=liquidity,
                     pool_count=pools, confidence=confidence)


def _stub_dexscreener(monkeypatch, info):
    from tools.defi.providers import dexscreener
    monkeypatch.setattr(dexscreener, "token", lambda chain, addr, **kw: info)


def test_the_fallback_price_refuses_a_price_with_no_measured_depth(monkeypatch):
    """A pool with no liquidity behind it is exactly the shape an attacker can
    seed — and it was accepted verbatim."""
    _stub_dexscreener(monkeypatch, _info(0.42, 0.0, 1, "low"))
    assert _bare_tool()._fallback_price("base", USDC) is None


def test_the_fallback_price_refuses_a_price_with_no_liquidity_datum(monkeypatch):
    """Unknown depth is not zero risk — it is an unread measurement."""
    _stub_dexscreener(monkeypatch, _info(0.42, None, 1, "low"))
    assert _bare_tool()._fallback_price("base", USDC) is None


def test_the_fallback_price_refuses_an_unknown_confidence(monkeypatch):
    _stub_dexscreener(monkeypatch, _info(0.42, 90_000.0, 3, "unknown"))
    assert _bare_tool()._fallback_price("base", USDC) is None


def test_the_fallback_price_still_accepts_a_LOW_confidence_price_with_depth(monkeypatch):
    """The exemption exists for a position that LOST the high-confidence bar.
    Gating the fallback at that same bar would re-break every such exit."""
    _stub_dexscreener(monkeypatch, _info(0.42, 25_000.0, 1, "low"))
    assert _bare_tool()._fallback_price("base", USDC) == 0.42


def test_the_fallback_price_accepts_a_high_confidence_price(monkeypatch):
    _stub_dexscreener(monkeypatch, _info(0.42, 90_000.0, 3, "high"))
    assert _bare_tool()._fallback_price("base", USDC) == 0.42


def test_an_injected_fallback_pricer_is_unchanged(monkeypatch):
    """The seam tests and the bridge verbs inject their own pricer; the gate is
    on the DEFAULT body, not on what a caller supplies."""
    tool = DefiTradeTool(wallet=_Wallet(_Gate()), rail_factory=_Rail,
                         fallback_price_fn=lambda c, a: 7.0)
    assert tool._fallback_price("base", USDC) == 7.0
