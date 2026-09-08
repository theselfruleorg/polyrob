"""The Solana swap verb. Phase 3 — the first Solana path that can move value.

Every gate the EVM verbs have, re-expressed for a chain with no allowances:
default-off flag, dry_run default true, declared USD ceiling, simulation +
delta assertion, the authority taxonomy in place of an allowance check, the
fee-payer perimeter, and the same PolicyGate caps.
"""
import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from tools.defi.trade_tool import DefiTradeTool, SolanaSwapParams

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
ME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"


def _params(**kw):
    base = dict(token_in=USDC, token_out=WSOL, amount_in=1.0, max_spend_usd=2.0)
    base.update(kw)
    return SolanaSwapParams(**base)


@pytest.mark.asyncio
async def test_the_verb_is_off_by_default(monkeypatch):
    """Shipping the rail must change nothing until an operator arms it."""
    monkeypatch.delenv("SOLANA_TRADE_ENABLED", raising=False)
    res = await DefiTradeTool().solana_swap(_params())
    assert res.error and "SOLANA_TRADE_ENABLED" in res.error


@pytest.mark.asyncio
async def test_it_defaults_to_dry_run(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    assert _params().dry_run is True


@pytest.mark.asyncio
async def test_an_evm_address_is_refused_on_solana(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    res = await DefiTradeTool().solana_swap(
        _params(token_in="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"))
    assert res.error


@pytest.mark.asyncio
async def test_identical_tokens_are_refused(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    res = await DefiTradeTool().solana_swap(_params(token_out=USDC))
    assert res.error and "same" in res.error.lower()


@pytest.mark.asyncio
async def test_no_route_is_reported_as_unknown(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    tool = DefiTradeTool(wallet=_Wallet(), solana_quote_fn=lambda *a, **k: None)
    res = await tool.solana_swap(_params())
    assert res.error and ("no route" in res.error.lower()
                          or "unknown" in res.error.lower())


# -- the guard --------------------------------------------------------------

class _Gate:
    has_daily_cap = True
    def __init__(self):
        self.recorded = []
        self.checked = []
        self.check_result = None  # None => allowed
    def check(self, *, venue, amount_usd, idempotency_key):
        import types as _t
        self.checked.append((venue, amount_usd, idempotency_key))
        return self.check_result or _t.SimpleNamespace(allowed=True, reason=None)
    def record(self, **kw):
        self.recorded.append(kw)
    def reserve(self):
        class _C:
            async def __aenter__(s): return None
            async def __aexit__(s, *a): return False
        return _C()


class _Signer:
    address = ME
    def sign_transaction(self, tx): return tx


class _Wallet:
    def __init__(self): self.policy = _Gate()
    def solana_signer(self, account=0): return _Signer()
    @property
    def solana_address(self): return ME


def _quote(out=10_000_000, floor=9_900_000):
    from tools.defi.providers.jupiter import JupiterQuote
    return JupiterQuote(chain="solana", token_in=USDC, token_out=WSOL,
                        amount_in_raw=1_000_000, amount_out_raw=out,
                        amount_out_min_raw=floor, venue="jupiter:Orca",
                        raw={"outAmount": str(out),
                             "otherAmountThreshold": str(floor)})


@pytest.mark.asyncio
async def test_an_undeclared_authority_grant_refuses(monkeypatch):
    """The Solana replacement for the undeclared-Approval refusal. A swap that
    hands someone a delegate, a close authority or ownership is not a swap."""
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    from core.wallet.solana_simulation import SolanaDeltas
    deltas = SolanaDeltas(ok=True, token_deltas={USDC: -1_000_000},
                          authority_grants=(("delegate", USDC, "someone"),))
    tool = DefiTradeTool(wallet=_Wallet(),
                         solana_quote_fn=lambda *a, **k: _quote(),
                         solana_build_fn=lambda *a, **k: b"\x01",
                         solana_simulate_fn=lambda **k: deltas)
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error and ("authority" in res.error.lower()
                          or "delegate" in res.error.lower())


@pytest.mark.asyncio
async def test_a_failed_simulation_refuses(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    from core.wallet.solana_simulation import SolanaDeltas
    tool = DefiTradeTool(wallet=_Wallet(),
                         solana_quote_fn=lambda *a, **k: _quote(),
                         solana_build_fn=lambda *a, **k: b"\x01",
                         solana_simulate_fn=lambda **k: SolanaDeltas(False, "reverted"))
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error and "simul" in res.error.lower()


@pytest.mark.asyncio
async def test_a_clean_dry_run_broadcasts_nothing(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    from core.wallet.solana_simulation import SolanaDeltas
    sent = []
    tool = DefiTradeTool(wallet=_Wallet(),
                         solana_quote_fn=lambda *a, **k: _quote(),
                         solana_build_fn=lambda *a, **k: b"\x01",
                         solana_simulate_fn=lambda **k: SolanaDeltas(
                             ok=True, token_deltas={USDC: -1_000_000}),
                         solana_send_fn=lambda raw: sent.append(raw) or "sig")
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error is None
    assert "DRY RUN" in (res.extracted_content or "")
    assert sent == []


@pytest.mark.asyncio
async def test_an_unexplained_native_outflow_refuses(monkeypatch):
    """Rent is classified, not licensed. Half a SOL is not rent."""
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    from core.wallet.solana_simulation import SolanaDeltas
    tool = DefiTradeTool(wallet=_Wallet(),
                         solana_quote_fn=lambda *a, **k: _quote(),
                         solana_build_fn=lambda *a, **k: b"\x01",
                         solana_simulate_fn=lambda **k: SolanaDeltas(
                             ok=True, native_delta=-500_000_000,
                             token_deltas={USDC: -1_000_000}))
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error and "sol" in res.error.lower()


@pytest.mark.asyncio
async def test_rent_sized_native_movement_is_allowed(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    from core.wallet.solana_simulation import SolanaDeltas
    tool = DefiTradeTool(wallet=_Wallet(),
                         solana_quote_fn=lambda *a, **k: _quote(),
                         solana_build_fn=lambda *a, **k: b"\x01",
                         solana_simulate_fn=lambda **k: SolanaDeltas(
                             ok=True, native_delta=-2_100_000,
                             token_deltas={USDC: -1_000_000}))
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error is None


@pytest.mark.asyncio
async def test_a_loose_jupiter_floor_refuses(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    tool = DefiTradeTool(wallet=_Wallet(),
                         solana_quote_fn=lambda *a, **k: _quote(out=1_000_000, floor=1))
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error and ("slippage" in res.error.lower() or "floor" in res.error.lower())


# --------------------------------------------------------------------------
# Mint decimals must be READ, never guessed (2026-08-26)
#
# `_identity_solana` defaulted to 6 when the screener had none. That is a guess
# on a SIZING path: wSOL is 9, so a 0.003 SOL swap sized with 6 decimals becomes
# 0.000003 SOL — a 1000x error. EVM refuses in exactly this situation
# ("refusing to size a swap against a token whose denomination is unknown") and
# this was the one place the Solana path guessed instead.
# --------------------------------------------------------------------------

WSOL = "So11111111111111111111111111111111111111112"


def test_decimals_are_read_from_the_mint():
    tool = DefiTradeTool(solana_decimals_fn=lambda mint: 9)
    assert tool._identity_solana(WSOL) == 9


def test_an_unreadable_mint_refuses_rather_than_guessing():
    """None, so the caller refuses. A wrong denomination misprices by orders of
    magnitude, and 'probably 6' is not a denomination."""
    tool = DefiTradeTool(solana_decimals_fn=lambda mint: None)
    assert tool._identity_solana(WSOL) is None


@pytest.mark.asyncio
async def test_a_swap_refuses_when_decimals_cannot_be_read(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    tool = DefiTradeTool(wallet=_Wallet(), solana_decimals_fn=lambda mint: None)
    res = await tool.solana_swap(_params())
    assert res.error and "decimals" in res.error.lower()


@pytest.mark.asyncio
async def test_a_swap_sizes_from_the_REAL_decimals(monkeypatch):
    """9-decimal wSOL: 0.003 must become 3_000_000 raw, not 3_000."""
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    seen = {}

    def _quote_fn(ti, to_, amt, **kw):
        seen["amount"] = amt
        return None

    tool = DefiTradeTool(wallet=_Wallet(), solana_decimals_fn=lambda mint: 9,
                         solana_quote_fn=_quote_fn)
    await tool.solana_swap(_params(token_in=WSOL, token_out=USDC, amount_in=0.003))
    assert seen["amount"] == 3_000_000


# --------------------------------------------------------------------------
# The simulation must actually observe the token accounts (2026-08-26)
#
# Caught on the first real mainnet dry run: the verb reported
# "simulated: token deltas {}, native 0 lamports" and PASSED. It passed because
# it saw nothing, not because it verified something — the simulation requested
# the owner's SYSTEM account, while SPL balances live in token accounts the
# owner merely owns. That is the "a simulation that did not run is not a
# simulation that passed" failure, one layer down.
# --------------------------------------------------------------------------

def test_the_simulation_requests_the_token_accounts_not_just_the_owner():
    """The address set moved to `core.wallet.solana_tx_inspect` (Token-2022 ATA
    derivation + every wallet-owned token account); the invariant travels with
    it — the owner's system account holds no SPL balance, so observing it alone
    asserts nothing."""
    from core.wallet import solana_tx_inspect

    addresses = solana_tx_inspect.simulation_addresses(
        ME, mints=[USDC], rpc=lambda method, params: {"value": []})
    assert len(addresses) > 1, "the owner alone asserts nothing"
    assert any(a != ME for a in addresses), "the ATAs must be named"


@pytest.mark.asyncio
async def test_a_swap_whose_simulation_shows_NO_token_movement_refuses(monkeypatch):
    """A swap that moves nothing is not a swap. Passing on an empty delta set
    is indistinguishable from passing on an unobserved one."""
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    from core.wallet.solana_simulation import SolanaDeltas
    tool = DefiTradeTool(wallet=_Wallet(), solana_decimals_fn=lambda m: 6,
                         solana_quote_fn=lambda *a, **k: _quote(),
                         solana_build_fn=lambda *a, **k: b"\x01",
                         solana_simulate_fn=lambda **k: SolanaDeltas(
                             ok=True, token_deltas={}, native_delta=0))
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error and ("no token movement" in res.error.lower()
                          or "observed nothing" in res.error.lower())


@pytest.mark.asyncio
async def test_a_swap_with_real_observed_movement_passes(monkeypatch):
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    from core.wallet.solana_simulation import SolanaDeltas
    tool = DefiTradeTool(wallet=_Wallet(), solana_decimals_fn=lambda m: 6,
                         solana_quote_fn=lambda *a, **k: _quote(),
                         solana_build_fn=lambda *a, **k: b"\x01",
                         solana_simulate_fn=lambda **k: SolanaDeltas(
                             ok=True, token_deltas={USDC: -1_000_000},
                             native_delta=0))
    res = await tool.solana_swap(_params(dry_run=True))
    assert res.error is None
