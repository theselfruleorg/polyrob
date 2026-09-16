"""solana_swap guard parity (2026-08-27 crypto finalization).

The verb's docstring promised "the same PolicyGate caps" while the body never
read max_spend_usd, never probed the kill switch, never checked turn origin,
never consulted PolicyGate and never recorded a spend. These tests pin the
mirror of tx_guard's step order onto the SVM path: kill switch → turn origin
(incl. the DEFI_AUTONOMOUS_TURN_TRADING and DEFI_MONITOR_EXITS lanes) →
simulation/delta assertions → valuation → declared ceiling → PolicyGate →
autonomous ceiling → daily-cap-required → pinned-RPC broadcast bar → record.
"""
import types

import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from tools.defi.trade_tool import DefiTradeTool, SolanaSwapParams

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
MEME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
ME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"


class _Gate:
    has_daily_cap = True

    def __init__(self):
        self.recorded = []
        self.checked = []
        self.check_result = None

    def check(self, *, venue, amount_usd, idempotency_key):
        self.checked.append((venue, amount_usd, idempotency_key))
        return self.check_result or types.SimpleNamespace(allowed=True, reason=None)

    def record(self, **kw):
        self.recorded.append(kw)

    def reserve(self):
        class _C:
            async def __aenter__(s): return None
            async def __aexit__(s, *a): return False
        return _C()


class _Signer:
    address = ME

    def sign_transaction(self, tx):
        return tx


class _Wallet:
    def __init__(self):
        self.policy = _Gate()

    def solana_signer(self, account=0):
        return _Signer()

    @property
    def solana_address(self):
        return ME


def _quote(out=10_000_000, floor=9_900_000):
    from tools.defi.providers.jupiter import JupiterQuote
    return JupiterQuote(chain="solana", token_in=USDC, token_out=WSOL,
                        amount_in_raw=1_000_000, amount_out_raw=out,
                        amount_out_min_raw=floor, venue="jupiter:Orca",
                        raw={"outAmount": str(out),
                             "otherAmountThreshold": str(floor)})


def _params(**kw):
    base = dict(token_in=USDC, token_out=WSOL, amount_in=1.0, max_spend_usd=2.0)
    base.update(kw)
    return SolanaSwapParams(**base)


def _tool(wallet=None, *, deltas=None, **kw):
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = deltas or SolanaDeltas(ok=True, token_deltas={USDC: -1_000_000})
    defaults = dict(
        wallet=wallet or _Wallet(),
        solana_decimals_fn=lambda m: 6,
        solana_quote_fn=lambda *a, **k: _quote(),
        solana_build_fn=lambda *a, **k: b"\x01",
        solana_simulate_fn=lambda **k: deltas,
    )
    defaults.update(kw)
    return DefiTradeTool(**defaults)


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    monkeypatch.delenv("DEFI_AUTONOMOUS_TURN_TRADING", raising=False)
    monkeypatch.delenv("DEFI_MONITOR_EXITS", raising=False)
    monkeypatch.delenv("DEFI_SOLANA_RPC", raising=False)


# -- declared ceiling -------------------------------------------------------

@pytest.mark.asyncio
async def test_max_spend_usd_is_enforced(monkeypatch):
    """Pinned USDC values at $1.00 by definition; $3 vs a $2 declaration refuses."""
    tool = _tool()
    res = await tool.solana_swap(_params(amount_in=3.0, max_spend_usd=2.0))
    assert res.error and "max_spend_usd" in res.error


@pytest.mark.asyncio
async def test_within_declared_ceiling_passes_dry(monkeypatch):
    tool = _tool()
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error is None
    assert "valued: $1.00" in (res.extracted_content or "")


# -- PolicyGate -------------------------------------------------------------

@pytest.mark.asyncio
async def test_policygate_refusal_blocks(monkeypatch):
    wallet = _Wallet()
    wallet.policy.check_result = types.SimpleNamespace(
        allowed=False, reason="daily spend cap $25.00 would be exceeded")
    tool = _tool(wallet)
    res = await tool.solana_swap(_params(dry_run=True))
    text = res.extracted_content or ""
    assert "PolicyGate" in text and "NOT SENT" in text


@pytest.mark.asyncio
async def test_the_gate_sees_the_valued_amount(monkeypatch):
    wallet = _Wallet()
    tool = _tool(wallet)
    await tool.solana_swap(_params(amount_in=1.5, max_spend_usd=2.0))
    assert wallet.policy.checked
    venue, amount_usd, idem = wallet.policy.checked[0]
    assert venue == "defi"
    assert amount_usd == 1.5
    assert idem.startswith("defi_solana_swap:")


# -- kill switch + turn origin ---------------------------------------------

@pytest.mark.asyncio
async def test_kill_switch_refuses(monkeypatch):
    monkeypatch.setattr("core.wallet.tx_guard._halted", lambda: True)
    tool = _tool()
    res = await tool.solana_swap(_params())
    assert res.error and "HALTED" in res.error


@pytest.mark.asyncio
async def test_entry_pause_refuses_a_plain_swap(monkeypatch):
    """2026-08-28: the entry-pause mirror. A plain buy (not a sell of a held
    token into USDC) is refused while entries are paused."""
    monkeypatch.setattr("core.wallet.tx_guard._entry_paused", lambda: True)
    tool = _tool()
    res = await tool.solana_swap(_params())
    assert res.error and "pause" in res.error.lower()


@pytest.mark.asyncio
async def test_entry_pause_allows_an_exit_shaped_swap(monkeypatch):
    """A sell of the held token into USDC still runs while paused."""
    from core.wallet.solana_simulation import SolanaDeltas
    monkeypatch.setattr("core.wallet.tx_guard._entry_paused", lambda: True)
    monkeypatch.setattr(
        "tools.defi.trade_tool.DefiTradeTool._solana_usdc_mint", lambda self: USDC)
    monkeypatch.setattr(
        "tools.defi.trade_tool.DefiTradeTool._solana_held_raw",
        lambda self, owner, mint: 1_000_000)
    deltas = SolanaDeltas(ok=True, token_deltas={WSOL: -1_000_000, USDC: 1_000_000})
    tool = _tool(deltas=deltas)
    res = await tool.solana_swap(_params(token_in=WSOL, token_out=USDC,
                                         max_spend_usd=200.0, dry_run=True))
    assert res.error is None, res.error


@pytest.mark.asyncio
async def test_forged_turn_refuses(monkeypatch):
    monkeypatch.setattr(
        "tools.controller.action_registration._is_forged_or_autonomous_turn",
        lambda ctx, tool: True)
    ctx = types.SimpleNamespace(user_id="owner", is_sub_agent=True, role="leaf")
    tool = _tool()
    res = await tool.solana_swap(_params(), execution_context=ctx)
    assert res.error and "forged" in res.error.lower()


@pytest.mark.asyncio
async def test_autonomous_goal_turn_allowed_when_armed(monkeypatch):
    monkeypatch.setenv("DEFI_AUTONOMOUS_TURN_TRADING", "true")
    monkeypatch.setattr(
        "tools.controller.action_registration._is_forged_or_autonomous_turn",
        lambda ctx, tool: True)
    monkeypatch.setattr(
        "tools.controller.action_registration._is_autonomous_goal_turn",
        lambda ctx, tool: True)
    ctx = types.SimpleNamespace(user_id="owner", is_sub_agent=False, role="orchestrator")
    tool = _tool()
    res = await tool.solana_swap(_params(dry_run=True), execution_context=ctx)
    assert res.error is None


@pytest.mark.asyncio
async def test_autonomous_origin_demands_a_daily_cap(monkeypatch):
    monkeypatch.setenv("DEFI_AUTONOMOUS_TURN_TRADING", "true")
    monkeypatch.setattr(
        "tools.controller.action_registration._is_forged_or_autonomous_turn",
        lambda ctx, tool: True)
    monkeypatch.setattr(
        "tools.controller.action_registration._is_autonomous_goal_turn",
        lambda ctx, tool: True)
    ctx = types.SimpleNamespace(user_id="owner", is_sub_agent=False, role="orchestrator")
    wallet = _Wallet()
    wallet.policy.has_daily_cap = False
    tool = _tool(wallet)
    res = await tool.solana_swap(_params(dry_run=True), execution_context=ctx)
    text = res.extracted_content or ""
    assert "WALLET_DAILY_CAP_USD" in text and "NOT SENT" in text


# -- the monitor-exit lane --------------------------------------------------

def _monitor_ctx():
    return types.SimpleNamespace(user_id="owner", is_sub_agent=False, role="orchestrator")


def _forged(monkeypatch):
    monkeypatch.setattr(
        "tools.controller.action_registration._is_forged_or_autonomous_turn",
        lambda ctx, tool: True)
    monkeypatch.setattr(
        "tools.controller.action_registration._is_autonomous_goal_turn",
        lambda ctx, tool: False)


@pytest.mark.asyncio
async def test_monitor_exit_sell_to_usdc_is_allowed(monkeypatch):
    """A forged MAIN-agent turn may CLOSE a held position into USDC when
    DEFI_MONITOR_EXITS is armed — valued at the measured receipt when the
    outflow is unpriceable."""
    monkeypatch.setenv("DEFI_MONITOR_EXITS", "true")
    _forged(monkeypatch)
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, token_deltas={MEME: -5_000_000,
                                                 USDC: 4_000_000})
    tool = _tool(deltas=deltas,
                 price_fn=lambda c, a: None,
                 fallback_price_fn=lambda c, a: None,
                 solana_held_fn=lambda owner, mint: 10_000_000)
    res = await tool.solana_swap(
        _params(token_in=MEME, token_out=USDC, amount_in=5.0,
                max_spend_usd=5.0, dry_run=True),
        execution_context=_monitor_ctx())
    assert res.error is None, res.error
    assert "valued: $4.00" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_monitor_exit_requires_a_measured_inflow(monkeypatch):
    monkeypatch.setenv("DEFI_MONITOR_EXITS", "true")
    _forged(monkeypatch)
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, token_deltas={MEME: -5_000_000})
    tool = _tool(deltas=deltas,
                 price_fn=lambda c, a: 1.0,
                 solana_held_fn=lambda owner, mint: 10_000_000)
    res = await tool.solana_swap(
        _params(token_in=MEME, token_out=USDC, amount_in=5.0,
                max_spend_usd=6.0, dry_run=True),
        execution_context=_monitor_ctx())
    assert res.error and "measured inflow" in res.error


@pytest.mark.asyncio
async def test_monitor_exit_does_not_admit_an_entry(monkeypatch):
    """Buying a meme with USDC is not exit-shaped — the forged bar holds."""
    monkeypatch.setenv("DEFI_MONITOR_EXITS", "true")
    _forged(monkeypatch)
    tool = _tool(solana_held_fn=lambda owner, mint: 10_000_000)
    res = await tool.solana_swap(
        _params(token_in=USDC, token_out=MEME), execution_context=_monitor_ctx())
    assert res.error and "forged" in res.error.lower()


# -- delta assertion (sell side) --------------------------------------------

@pytest.mark.asyncio
async def test_outflow_exceeding_declared_refuses(monkeypatch):
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, token_deltas={USDC: -2_500_000})
    tool = _tool(deltas=deltas)
    res = await tool.solana_swap(_params(amount_in=1.0, max_spend_usd=5.0))
    assert res.error and "more than the declared" in res.error


# -- valuation refusals ------------------------------------------------------

@pytest.mark.asyncio
async def test_unpriceable_outflow_refuses(monkeypatch):
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, token_deltas={MEME: -5_000_000})
    tool = _tool(deltas=deltas,
                 price_fn=lambda c, a: None,
                 fallback_price_fn=lambda c, a: None,
                 solana_held_fn=lambda owner, mint: None)
    res = await tool.solana_swap(
        _params(token_in=MEME, token_out=WSOL, amount_in=5.0))
    assert res.error and "valued" in res.error


# -- broadcast: pinned RPC + audit record ------------------------------------

@pytest.mark.asyncio
async def test_broadcast_requires_a_pinned_rpc(monkeypatch):
    tool = _tool()
    res = await tool.solana_swap(_params(dry_run=False))
    assert res.error and "DEFI_SOLANA_RPC" in res.error


@pytest.mark.asyncio
async def test_broadcast_records_the_spend(monkeypatch):
    monkeypatch.setenv("DEFI_SOLANA_RPC", "https://solana.example/rpc")
    wallet = _Wallet()
    sent = []
    # The confirmation seam is injected: without it this test spends 60s
    # polling a nonexistent RPC (and, before the seam existed, the verb
    # reported BROADCAST without ever checking that the swap landed).
    tool = _tool(wallet, solana_send_fn=lambda raw: sent.append(raw) or "sig123",
                 solana_confirm_fn=lambda sig: (True, "finalized"))
    res = await tool.solana_swap(_params(dry_run=False))
    assert res.error is None
    assert "RESULT: CONFIRMED" in (res.extracted_content or "")
    assert sent == [b"\x01"]
    assert wallet.policy.recorded
    rec = wallet.policy.recorded[0]
    assert rec["venue"] == "defi"
    assert rec["action"] == "solana_swap"
    assert rec["amount_usd"] == 1.0
    assert rec["result_ref"] == "sig123"
