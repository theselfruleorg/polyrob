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


def _quote(out=1_000_000, floor=990_000, *, ti=USDC, to=WSOL, amt=1_000_000):
    # CR-H03: a quote must be for THE request (mints + amount), and its floor
    # is asserted against the simulated receipt — so the fixture quotes what
    # was asked and the default deltas below deliver at least the floor.
    from tools.defi.providers.jupiter import JupiterQuote
    return JupiterQuote(chain="solana", token_in=ti, token_out=to,
                        amount_in_raw=amt, amount_out_raw=out,
                        amount_out_min_raw=floor, venue="jupiter:Orca",
                        raw={"outAmount": str(out),
                             "otherAmountThreshold": str(floor)})


def _clean_screen():
    from tools.defi.providers.base import ScreenVerdict
    return ScreenVerdict(available=True, checks={"transfer_fee": "no"})


def _params(**kw):
    base = dict(token_in=USDC, token_out=WSOL, amount_in=1.0, max_spend_usd=2.0)
    base.update(kw)
    return SolanaSwapParams(**base)


def _tool(wallet=None, *, deltas=None, **kw):
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = deltas or SolanaDeltas(ok=True, fee_lamports=5_000, token_deltas={USDC: -1_000_000},
                                    native_delta=1_000_000)
    defaults = dict(
        wallet=wallet or _Wallet(),
        solana_decimals_fn=lambda m: 6,
        solana_quote_fn=lambda ti, to, amt, **k: _quote(ti=ti, to=to, amt=amt),
        solana_build_fn=lambda *a, **k: b"\x01",
        solana_simulate_fn=lambda **k: deltas,
        # CR-M05: the blockhash check reads the pinned RPC; fixtures say valid.
        solana_blockhash_fn=lambda raw: (True, "valid"),
        # CR-L10: a buy of a non-pinned mint is screened; fixtures screen clean.
        solana_screen_fn=lambda mint: _clean_screen(),
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
    assert res.error and "paused" in res.error and "/resume" in res.error
    assert "(owner kill-switch)" not in res.error


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
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, token_deltas={WSOL: -1_000_000, USDC: 1_000_000})
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
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, token_deltas={MEME: -5_000_000,
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
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, token_deltas={MEME: -5_000_000})
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
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, token_deltas={USDC: -2_500_000})
    tool = _tool(deltas=deltas)
    res = await tool.solana_swap(_params(amount_in=1.0, max_spend_usd=5.0))
    assert res.error and "more than the declared" in res.error


# -- valuation refusals ------------------------------------------------------

@pytest.mark.asyncio
async def test_unpriceable_outflow_refuses(monkeypatch):
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, token_deltas={MEME: -5_000_000},
                          native_delta=1_000_000)
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


# -- CR-H03: what ARRIVES is asserted, not only what leaves -------------------

@pytest.mark.asyncio
async def test_cr_h03_a_route_that_takes_and_returns_nothing_refuses():
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, token_deltas={USDC: -1_000_000, MEME: 0})
    tool = _tool(deltas=deltas)
    res = await tool.solana_swap(_params(token_out=MEME, dry_run=True))
    assert res.error and "below the route's floor" in res.error


@pytest.mark.asyncio
async def test_cr_h03_an_unobserved_output_mint_refuses():
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, token_deltas={USDC: -1_000_000})
    tool = _tool(deltas=deltas)
    res = await tool.solana_swap(_params(token_out=MEME, dry_run=True))
    assert res.error and "could not observe the token you are buying" in res.error


@pytest.mark.asyncio
async def test_cr_h03_a_sol_receipt_below_the_floor_refuses():
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, token_deltas={USDC: -1_000_000},
                          native_delta=-5_000)
    tool = _tool(deltas=deltas)
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error and "below the route's floor" in res.error


@pytest.mark.asyncio
async def test_cr_h03_a_receipt_at_the_floor_passes():
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, token_deltas={USDC: -1_000_000, MEME: 990_000})
    tool = _tool(deltas=deltas)
    res = await tool.solana_swap(_params(token_out=MEME, dry_run=True))
    assert res.error is None, res.error


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    dict(ti=MEME), dict(to=MEME), dict(amt=999_999)])
async def test_cr_h03_a_quote_for_another_trade_refuses(override):
    kw = dict(ti=USDC, to=WSOL, amt=1_000_000) | override
    tool = _tool(solana_quote_fn=lambda *a, **k: _quote(**kw))
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error and "does not match the request" in res.error


# -- CR-M05: the blockhash is checked before signing -------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("check", [
    lambda raw: (False, "isBlockhashValid=False"),
    lambda raw: (_ for _ in ()).throw(RuntimeError("rpc down")),
])
async def test_cr_m05_an_invalid_or_unchecked_blockhash_is_never_signed(monkeypatch, check):
    monkeypatch.setenv("DEFI_SOLANA_RPC", "https://solana.example/rpc")
    wallet = _Wallet()
    sent = []
    tool = _tool(wallet, solana_send_fn=lambda raw: sent.append(raw) or "sig",
                 solana_confirm_fn=lambda sig: (True, "finalized"),
                 solana_blockhash_fn=check)
    res = await tool.solana_swap(_params(dry_run=False))
    assert res.error and "blockhash" in res.error
    assert sent == [] and not wallet.policy.recorded


def test_cr_m05_the_default_check_asks_the_rpc_for_the_embedded_blockhash(monkeypatch):
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    from core.wallet import solana_rail

    kp = Keypair()
    bh = Hash.new_unique()
    msg = MessageV0.try_compile(kp.pubkey(), [], [], bh)
    raw = bytes(VersionedTransaction(msg, [kp]))
    calls = []

    def rpc(self, method, params, timeout=10.0):
        calls.append((method, params))
        return {"value": False}
    monkeypatch.setattr(solana_rail.SolanaRail, "_rpc", rpc)
    ok, detail = DefiTradeTool()._solana_blockhash_valid(raw)
    assert not ok
    assert calls[0][0] == "isBlockhashValid" and calls[0][1][0] == str(bh)


# -- CR-L06: native SOL beyond fee + retained rent is charged ----------------

@pytest.mark.asyncio
async def test_cr_l06_native_excess_is_charged_to_the_caps():
    from core.wallet.solana_simulation import SolanaDeltas
    wallet = _Wallet()
    # 0.009 SOL leaves beside a USDC->MEME buy: under the 0.01 "rent" ceiling,
    # but only 5,000 lamports of it is the fee.
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, native_delta=-9_005_000,
                          token_deltas={USDC: -1_000_000, MEME: 990_000})
    tool = _tool(wallet, deltas=deltas, price_fn=lambda c, a: 100.0)
    res = await tool.solana_swap(_params(token_out=MEME, max_spend_usd=5.0, dry_run=True))
    assert res.error is None, res.error
    # $1.00 of USDC + 0.009 SOL * $100 = $1.90
    assert wallet.policy.checked[0][1] == 1.9


@pytest.mark.asyncio
async def test_cr_l06_retained_rent_is_not_charged():
    from core.wallet.solana_simulation import SolanaDeltas
    wallet = _Wallet()
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, retained_rent_lamports=2_039_280,
                          native_delta=-2_044_280,
                          token_deltas={USDC: -1_000_000, MEME: 990_000})
    tool = _tool(wallet, deltas=deltas, price_fn=lambda c, a: 100.0)
    res = await tool.solana_swap(_params(token_out=MEME, dry_run=True))
    assert res.error is None, res.error
    assert wallet.policy.checked[0][1] == 1.0


@pytest.mark.asyncio
async def test_cr_l06_an_unknown_fee_refuses():
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, native_delta=1_000_000,
                          token_deltas={USDC: -1_000_000})
    tool = _tool(deltas=deltas)
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error and "fee" in res.error


@pytest.mark.asyncio
async def test_cr_l06_an_unpriceable_excess_refuses():
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000, native_delta=-1_005_000,
                          token_deltas={USDC: -1_000_000, MEME: 990_000})
    tool = _tool(deltas=deltas, price_fn=lambda c, a: None)
    res = await tool.solana_swap(_params(token_out=MEME, dry_run=True))
    assert res.error and "no trustworthy price" in res.error


# -- CR-L10: a buy is screened for ACTIVE Token-2022 traps -------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["transfer_fee_active", "transfer_hook_active",
                                  "default_account_frozen", "non_transferable",
                                  "permanent_delegate"])
async def test_cr_l10_a_blocking_flag_refuses_the_buy(flag):
    from tools.defi.providers.base import ScreenVerdict
    quoted = []
    tool = _tool(solana_screen_fn=lambda m: ScreenVerdict(available=True, flags=[flag]),
                 solana_quote_fn=lambda *a, **k: quoted.append(a))
    res = await tool.solana_swap(_params(token_out=MEME, dry_run=True))
    assert res.error and flag in res.error
    assert not quoted


@pytest.mark.asyncio
async def test_cr_l10_an_unavailable_screen_refuses_the_buy():
    from tools.defi.providers.base import ScreenVerdict
    tool = _tool(solana_screen_fn=lambda m: ScreenVerdict(available=False))
    res = await tool.solana_swap(_params(token_out=MEME, dry_run=True))
    assert res.error and "UNAVAILABLE" in res.error


@pytest.mark.asyncio
async def test_cr_l10_informational_flags_do_not_refuse():
    from core.wallet.solana_simulation import SolanaDeltas
    from tools.defi.providers.base import ScreenVerdict
    deltas = SolanaDeltas(ok=True, fee_lamports=5_000,
                          token_deltas={USDC: -1_000_000, MEME: 990_000})
    tool = _tool(deltas=deltas, solana_screen_fn=lambda m: ScreenVerdict(
        available=True, flags=["mintable", "freezable"]))
    res = await tool.solana_swap(_params(token_out=MEME, dry_run=True))
    assert res.error is None, res.error


@pytest.mark.asyncio
async def test_cr_l10_a_sell_into_the_pinned_quote_asset_is_not_screened():
    def boom(m):
        raise AssertionError("pinned quote assets are not screened")
    tool = _tool(solana_screen_fn=boom)
    res = await tool.solana_swap(_params(dry_run=True))      # USDC -> wSOL
    assert res.error is None, res.error
