"""The launchpad tool's own gates (042), above the guard.

The flag, the leaf refusal, the snipe-tax refusal, the curve-provenance rule,
and the declaration each verb hands to tx_guard.
"""
import contextlib

import pytest

from core.wallet.tx_guard import Decision
from tools.launchpad import pons, pons_abi as P
from tools.launchpad.tool import (
    LaunchParams, LaunchpadTool, QuoteParams, StatusParams, TradeParams)

HOLDER = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
TOKEN = "0xD8c32C1585758Bd7505F9ceA9C977a4294873ab2"
CURVE = "0x526fce0f274615695073fd3a09a54f2646DF1E00"


class _Gate:
    def __init__(self):
        self.recorded = []

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def record(self, **kw):
        self.recorded.append(kw)


class _Signer:
    address = HOLDER


class _Wallet:
    def __init__(self, gate):
        self.policy = gate

    def operational_signer(self):
        return _Signer()


class _Rail:
    last = None

    def __init__(self, chain, signer, **kw):
        self.built = None
        self.sent = False
        _Rail.last = self

    def build_call(self, *, to, data, value=0):
        self.built = {"to": to, "data": data, "value": value, "nonce": 3}
        return self.built

    def size_gas(self, tx, sim_gas_used):
        return {**tx, "gas": sim_gas_used}

    def sign_and_send(self, tx):
        self.sent = True
        return "0x" + "cd" * 32

    def await_receipt(self, tx_hash, **kw):
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status="success", block_number=9,
                       gas_used=300_000)

    def _rpc(self, method, params, **kw):
        return {"logs": [{"topics": [P.TOPIC_TOKEN_LAUNCHED,
                                     "0x" + "00" * 12 + "11" * 20,
                                     "0x" + "00" * 12 + "22" * 20,
                                     "0x" + "00" * 12 + "33" * 20],
                          "data": "0x"}]}


LIVE_STATE = dict(
    curve=CURVE, token=TOKEN,
    quote_reserve=1_697_385_058_099_346_349,
    token_reserve=989_757_740_580_789_077_404_031_820,
    reserved_tokens=285_714_285_714_285_714_285_714_285,
    fee_bps=100, creator_tax_bps=0, snipe_tax_bps=0, graduated=False)

TERMS = pons.LaunchTerms(
    enabled=True, launch_fee_wei=500_000_000_000_000, supply_raw=10 ** 27,
    curve_fee_bps=100, phantom_quote=168 * 10 ** 16,
    graduation_threshold=42 * 10 ** 17, economics="0x" + "a9" * 32,
    pair_decimals=18)


def _tool(monkeypatch, *, captured=None, allow=True, state_over=None,
          record_over=None, terms=TERMS):
    gate = _Gate()

    def _guard(intent, tx, **kw):
        if captured is not None:
            captured.append(intent)
        return Decision(allowed=allow, reason="test", lane="autonomous",
                        amount_usd=2.0, sim_gas_used=250_000)

    monkeypatch.setattr(pons, "verify_pins", lambda rpc: None)
    monkeypatch.setattr(pons, "read_terms", lambda rpc, **kw: terms)
    record = {"token": TOKEN, "curve": CURVE, "deployer": HOLDER,
              "pairToken": P.NATIVE_PAIR, "graduationThreshold": 42 * 10 ** 17,
              "phase": 0, "exists": True}
    record.update(record_over or {})
    monkeypatch.setattr(pons, "launched_token",
                        lambda rpc, token: None if record is None else record)
    monkeypatch.setattr(
        pons, "curve_state",
        lambda rpc, curve, *, recipient: pons.CurveState(
            **{**LIVE_STATE, **(state_over or {})}))

    tool = LaunchpadTool("launchpad", config=None, container=None,
                         wallet=_Wallet(gate), rail_factory=_Rail,
                         guard_fn=_guard, price_fn=lambda c, a: 2500.0,
                         rpc_fn=lambda m, p: None)
    return tool, gate


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_ENABLED", "true")


class _Leaf:
    role = "leaf"
    is_sub_agent = False
    user_id = "owner"
    metadata = {}


# ==========================================================================
# Gates
# ==========================================================================

@pytest.mark.asyncio
async def test_the_tool_is_off_by_default(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_ENABLED", raising=False)
    tool, _ = _tool(monkeypatch)
    res = await tool.launchpad_launch(LaunchParams(
        name="R", symbol="R", max_spend_usd=5.0))
    assert "LAUNCHPAD_ENABLED" in (res.error or "")


@pytest.mark.asyncio
async def test_a_leaf_never_launches(monkeypatch):
    tool, _ = _tool(monkeypatch)
    res = await tool.launchpad_launch(
        LaunchParams(name="R", symbol="R", max_spend_usd=5.0), _Leaf())
    assert "sub-agent" in (res.error or "")


@pytest.mark.asyncio
async def test_a_leaf_never_buys(monkeypatch):
    tool, _ = _tool(monkeypatch)
    res = await tool.launchpad_buy(
        TradeParams(token=TOKEN, amount=0.01, max_spend_usd=5.0), _Leaf())
    assert "sub-agent" in (res.error or "")


@pytest.mark.asyncio
async def test_a_disabled_factory_refuses(monkeypatch):
    tool, _ = _tool(monkeypatch, terms=pons.LaunchTerms(
        **{**TERMS.__dict__, "enabled": False}))
    res = await tool.launchpad_launch(LaunchParams(
        name="R", symbol="R", max_spend_usd=5.0))
    assert "launches DISABLED" in (res.error or "")


# ==========================================================================
# Curve provenance
# ==========================================================================

@pytest.mark.asyncio
async def test_a_token_the_factory_never_launched_is_refused(monkeypatch):
    """The curve comes from the factory's record and from nowhere else."""
    tool, _ = _tool(monkeypatch)
    monkeypatch.setattr(pons, "launched_token", lambda rpc, token: None)
    res = await tool.launchpad_buy(TradeParams(
        token="0x" + "99" * 20, amount=0.01, max_spend_usd=5.0))
    assert "factory has no record" in (res.error or "")


@pytest.mark.asyncio
async def test_a_graduated_token_points_at_the_swap_verb(monkeypatch):
    tool, _ = _tool(monkeypatch, state_over={"graduated": True})
    res = await tool.launchpad_buy(TradeParams(
        token=TOKEN, amount=0.01, max_spend_usd=5.0))
    assert "GRADUATED" in (res.error or "")
    assert "defi_trade.swap" in (res.error or "")


# ==========================================================================
# The snipe tax
# ==========================================================================

@pytest.mark.asyncio
async def test_a_buy_REFUSES_while_the_snipe_tax_is_live(monkeypatch):
    """It opens at 9900 bps. Quietly paying it is a total loss."""
    tool, _ = _tool(monkeypatch, state_over={"snipe_tax_bps": 9_900})
    res = await tool.launchpad_buy(TradeParams(
        token=TOKEN, amount=0.01, max_spend_usd=5.0))
    assert "snipe tax" in (res.error or "")
    assert "9900 bps" in (res.error or "")
    assert _Rail.last is None or _Rail.last.sent is False


@pytest.mark.asyncio
async def test_status_flags_a_live_snipe_tax(monkeypatch):
    tool, _ = _tool(monkeypatch, state_over={"snipe_tax_bps": 4_950})
    res = await tool.launchpad_status(StatusParams(token=TOKEN))
    assert "STILL ACTIVE" in res.extracted_content


# ==========================================================================
# What each verb DECLARES to the guard
# ==========================================================================

@pytest.mark.asyncio
async def test_a_launch_declares_a_native_outflow_of_fee_plus_buy(monkeypatch):
    captured = []
    tool, _ = _tool(monkeypatch, captured=captured)
    res = await tool.launchpad_launch(LaunchParams(
        name="Rob Coin", symbol="ROB", buy_amount=0.1, max_spend_usd=500.0,
        dry_run=True))
    assert res.error is None, res.error
    intent = captured[0]
    assert intent.token is None
    assert intent.amount_raw == 500_000_000_000_000 + 10 ** 17
    assert intent.to.lower() == P.ROUTER.lower()


@pytest.mark.asyncio
async def test_a_launch_without_an_opening_buy_goes_to_the_FACTORY(monkeypatch):
    captured = []
    tool, _ = _tool(monkeypatch, captured=captured)
    await tool.launchpad_launch(LaunchParams(
        name="R", symbol="R", buy_amount=0.0, max_spend_usd=5.0, dry_run=True))
    assert captured[0].to.lower() == P.FACTORY.lower()
    assert captured[0].amount_raw == 500_000_000_000_000


@pytest.mark.asyncio
async def test_a_buy_declares_the_MINIMUM_it_must_receive(monkeypatch):
    captured = []
    tool, _ = _tool(monkeypatch, captured=captured)
    res = await tool.launchpad_buy(TradeParams(
        token=TOKEN, amount=0.01, max_spend_usd=50.0, dry_run=True))
    assert res.error is None, res.error
    intent = captured[0]
    assert intent.token is None                   # native outflow
    assert intent.inflow_token == TOKEN
    assert intent.min_inflow_raw > 0


@pytest.mark.asyncio
async def test_a_sell_into_a_NATIVE_curve_declares_a_native_receipt(monkeypatch):
    """Without `min_native_inflow_wei` rule 6b refuses this shape outright —
    the same blind spot that made routes/lifi.py refuse every native quote."""
    captured = []
    tool, _ = _tool(monkeypatch, captured=captured)
    res = await tool.launchpad_sell(TradeParams(
        token=TOKEN, amount=1000.0, max_spend_usd=50.0, dry_run=True))
    assert res.error is None, res.error
    intent = captured[0]
    assert intent.token == TOKEN
    assert intent.min_native_inflow_wei > 0
    assert intent.min_inflow_raw is None
    assert [s.lower() for s in intent.watch_spenders] == [CURVE.lower()]


@pytest.mark.asyncio
async def test_a_sell_into_an_ERC20_curve_declares_a_TOKEN_receipt(monkeypatch):
    usdg = "0x5FC5360d0400a0fd4F2Af552aDD042d716f1d168"
    captured = []
    tool, _ = _tool(monkeypatch, captured=captured,
                    record_over={"pairToken": usdg})
    await tool.launchpad_sell(TradeParams(
        token=TOKEN, amount=1000.0, max_spend_usd=50.0, dry_run=True))
    intent = captured[0]
    assert intent.min_native_inflow_wei is None
    assert intent.inflow_token == usdg
    assert intent.min_inflow_raw > 0


@pytest.mark.asyncio
async def test_an_erc20_quoted_buy_is_refused_with_the_remedy(monkeypatch):
    tool, _ = _tool(monkeypatch, record_over={
        "pairToken": "0x5FC5360d0400a0fd4F2Af552aDD042d716f1d168"})
    res = await tool.launchpad_buy(TradeParams(
        token=TOKEN, amount=0.01, max_spend_usd=5.0))
    assert "approve_token" in (res.error or "")


# ==========================================================================
# Broadcast
# ==========================================================================

@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_broadcasts_nothing(monkeypatch):
    _Rail.last = None
    tool, _ = _tool(monkeypatch)
    res = await tool.launchpad_launch(LaunchParams(
        name="R", symbol="R", max_spend_usd=5.0))
    assert "DRY RUN" in res.extracted_content
    assert _Rail.last.sent is False


@pytest.mark.asyncio
async def test_a_live_launch_reports_the_address_from_the_receipt(monkeypatch):
    tool, gate = _tool(monkeypatch)
    res = await tool.launchpad_launch(LaunchParams(
        name="R", symbol="R", max_spend_usd=5.0, dry_run=False))
    assert "token: 0x" + "11" * 20 in res.extracted_content.replace(
        "0x1111111111111111111111111111111111111111",
        "0x" + "11" * 20)
    assert gate.recorded[0]["action"] == "launchpad_launch"
    # 043 A36: the durable spend record names the LAUNCHED TOKEN parsed from
    # the receipt, never the factory it was launched through.
    assert gate.recorded[0]["counterparty"].lower() == "0x" + "11" * 20
    assert gate.recorded[0]["chain"] == P.CHAIN


@pytest.mark.asyncio
async def test_no_TokenLaunched_event_falls_back_to_the_factory_address(monkeypatch):
    """When the receipt cannot be parsed for a TokenLaunched event, the spend
    record must still name a counterparty — the factory (`to`), never silently
    nothing — and the response still carries the explorer warning."""
    tool, gate = _tool(monkeypatch)
    monkeypatch.setattr(pons, "parse_token_launched", lambda logs: None)
    res = await tool.launchpad_launch(LaunchParams(
        name="R", symbol="R", max_spend_usd=5.0, dry_run=False))
    assert "check the explorer" in res.extracted_content
    assert gate.recorded[0]["action"] == "launchpad_launch"
    assert gate.recorded[0]["counterparty"] == _Rail.last.built["to"]
    assert gate.recorded[0]["chain"] == P.CHAIN


@pytest.mark.asyncio
async def test_a_refused_guard_broadcasts_nothing(monkeypatch):
    _Rail.last = None
    tool, _ = _tool(monkeypatch, allow=False)
    res = await tool.launchpad_launch(LaunchParams(
        name="R", symbol="R", max_spend_usd=5.0, dry_run=False))
    assert "NOT SENT" in res.extracted_content
    assert _Rail.last.sent is False


# ==========================================================================
# Reads
# ==========================================================================

@pytest.mark.asyncio
async def test_quote_prices_a_buy_without_a_wallet(monkeypatch):
    tool, _ = _tool(monkeypatch)
    res = await tool.launchpad_quote(QuoteParams(
        token=TOKEN, side="buy", amount=0.012345678901234567))
    assert res.error is None, res.error
    assert "RECEIVE" in res.extracted_content


@pytest.mark.asyncio
async def test_an_unknown_side_refuses(monkeypatch):
    tool, _ = _tool(monkeypatch)
    res = await tool.launchpad_quote(QuoteParams(
        token=TOKEN, side="hodl", amount=1.0))
    assert "buy" in (res.error or "")


@pytest.mark.asyncio
async def test_status_reports_graduation_progress(monkeypatch):
    tool, _ = _tool(monkeypatch)
    res = await tool.launchpad_status(StatusParams(token=TOKEN))
    assert "graduation:" in res.extracted_content
    assert "on the curve" in res.extracted_content
