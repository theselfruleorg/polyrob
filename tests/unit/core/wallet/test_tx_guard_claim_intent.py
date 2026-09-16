"""tx_guard — a CLAIM: sends nothing, receives what is already owed.

Found live 2026-09-14: the agent's two Pons launches had credited **13.613 ETH**
of creator tax to its own wallet in the launchpad's fee escrow, and no verb could
reach it. Its ledger recorded "launchpad tool has NO claim verb" and escalated to
the owner instead.

A claim is a THIRD intent shape, and every existing branch refused it for a
different reason:

  * step 3 refuses ``amount_raw == 0`` unless the intent is an allowance op or a
    deployment;
  * the native branch refuses ``moved > 0`` outright — "simulation shows a native
    INFLOW for a send";
  * the 042 native-inflow assertion is keyed on ``intent.token``, so on a
    ``token=None`` intent a declared minimum would have been silently SKIPPED,
    which is worse than refusing: a declaration that is not enforced reads like
    one that is.

So the shape is explicit. A claim MUST declare what it expects to receive — a
claim that asserts nothing is not a claim — and the simulation, not the caller,
decides whether it arrived.
"""
import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

ESCROW = "0xd3afeb2a57f70ef218aa82451c51b2fb0416ac9e"
HOLDER = "0xcAda546F6A6DdDe31b71Ab21ef63D3EbF09fA553"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

#: The live figure, to the wei.
CLAIMABLE = 13_613_005_871_733_144_000
ETH_PRICE = 2512.85


def _intent(**kw):
    base = dict(chain="robinhood", token=None, to=ESCROW, amount_raw=0,
                max_spend_usd=5.0, idempotency_key="claim-1",
                is_claim=True, min_native_inflow_wei=CLAIMABLE)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _deltas(native_delta=CLAIMABLE, **kw):
    base = dict(ok=True, native_delta=native_delta, token_deltas={},
                allowance_deltas={}, gas_used=42_546)
    base.update(kw)
    return Deltas(**base)


def _authorize(intent=None, deltas=None, *, price=ETH_PRICE, gate=None):
    return tx_guard.authorize(
        intent or _intent(),
        {"to": ESCROW, "data": "0x4e71d92d", "value": 0, "chainId": 4663,
         "gas": 63_819, "maxFeePerGas": 10 ** 9},
        holder=HOLDER,
        gate=gate or PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=1000.0),
        execution_context=None,
        simulate_fn=lambda **_: deltas if deltas is not None else _deltas(),
        price_fn=lambda chain, addr: price,
        fallback_price_fn=None,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False,
        entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False,
    )


# --- the happy path, which did not exist -----------------------------------

def test_a_claim_that_delivers_what_it_declared_is_allowed():
    d = _authorize()
    assert d.allowed, d.reason


def test_a_claim_costs_its_fee_never_zero():
    """Booking an inflow-only transaction at $0.00 is the confident-zero class
    that let 114 tenantless wallet_spend rows read as a treasury that had spent
    nothing. The wallet does pay gas."""
    d = _authorize()
    assert d.amount_usd is not None
    assert d.amount_usd > 0


def test_the_claim_cost_is_only_the_fee_not_the_receipt():
    """13.6 ETH ARRIVING must never be charged to the spend caps as if it left."""
    d = _authorize()
    assert d.amount_usd < 1.0, d.amount_usd


# --- it must assert something ---------------------------------------------

def test_a_claim_declaring_no_minimum_is_refused():
    d = _authorize(_intent(min_native_inflow_wei=None))
    assert not d.allowed
    assert "declare" in d.reason.lower() or "minimum" in d.reason.lower()


def test_a_claim_declaring_a_zero_minimum_is_refused():
    d = _authorize(_intent(min_native_inflow_wei=0))
    assert not d.allowed


# --- the simulation decides, not the caller -------------------------------

def test_a_claim_that_returns_nothing_is_refused():
    d = _authorize(deltas=_deltas(native_delta=0))
    assert not d.allowed
    assert "nothing" in d.reason.lower() or "no native" in d.reason.lower()


def test_a_claim_that_returns_less_than_declared_is_refused():
    d = _authorize(deltas=_deltas(native_delta=CLAIMABLE // 2))
    assert not d.allowed
    assert "below" in d.reason.lower()


def test_a_claim_whose_simulation_shows_an_OUTFLOW_is_refused():
    d = _authorize(deltas=_deltas(native_delta=-10 ** 15))
    assert not d.allowed


# --- nothing may leave ----------------------------------------------------

def test_a_claim_that_also_moves_a_token_out_is_refused():
    """A contract that pays you and quietly takes a token is not a claim."""
    d = _authorize(deltas=_deltas(token_deltas={USDC: -1_000_000}))
    assert not d.allowed
    assert "token" in d.reason.lower()


def test_a_claim_that_grants_an_undeclared_allowance_is_refused():
    d = _authorize(deltas=_deltas(
        allowance_deltas={(USDC, "0xbeef"): 10 ** 30}))
    assert not d.allowed


def test_a_token_inflow_alongside_a_native_claim_is_allowed():
    """Receiving MORE than declared is not a failure — it is a better receipt."""
    d = _authorize(deltas=_deltas(token_deltas={USDC: 5_000_000}))
    assert d.allowed, d.reason


# --- a TOKEN claim (claimToken) -------------------------------------------

def test_a_token_claim_asserts_its_declared_inflow():
    d = _authorize(_intent(min_native_inflow_wei=None,
                           inflow_token=USDC, min_inflow_raw=1_000_000),
                   deltas=_deltas(native_delta=0,
                                  token_deltas={USDC: 1_000_000}))
    assert d.allowed, d.reason


def test_a_token_claim_that_short_delivers_is_refused():
    d = _authorize(_intent(min_native_inflow_wei=None,
                           inflow_token=USDC, min_inflow_raw=1_000_000),
                   deltas=_deltas(native_delta=0,
                                  token_deltas={USDC: 999_999}))
    assert not d.allowed


# --- structure ------------------------------------------------------------

def test_a_claim_must_have_a_destination():
    d = _authorize(_intent(to=None))
    assert not d.allowed


def test_a_claim_cannot_also_be_a_deployment():
    d = _authorize(_intent(is_deploy=True))
    assert not d.allowed


def test_a_claim_declaring_an_outflow_amount_is_refused():
    """`amount_raw > 0` says value leaves. A claim's whole shape is that none does."""
    d = _authorize(_intent(amount_raw=10 ** 15))
    assert not d.allowed


# --- revalidation: the signed transaction must be the one authorized ------

def test_a_claim_whose_tx_goes_somewhere_else_is_refused():
    """The guard authorizes `intent.to` and the rail signs `tx["to"]`. For a
    deployment those are cross-checked explicitly; for everything else nothing
    compared them, and a claim PRINTS a provenance claim about its destination
    ("read FROM the curve, not supplied"). That sentence has to be true of the
    transaction that gets signed, not only of the intent that described it."""
    d = tx_guard.authorize(
        _intent(),
        {"to": "0x000000000000000000000000000000000000dEaD",
         "data": "0x4e71d92d", "value": 0, "chainId": 4663,
         "gas": 63_819, "maxFeePerGas": 10 ** 9},
        holder=HOLDER,
        gate=PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=1000.0),
        execution_context=None,
        simulate_fn=lambda **_: _deltas(),
        price_fn=lambda chain, addr: ETH_PRICE,
        fallback_price_fn=None,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False,
        entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False,
    )
    assert not d.allowed
    assert "destination" in d.reason.lower() or "declares" in d.reason.lower()


def test_a_claim_with_a_matching_destination_is_allowed_whatever_the_case():
    """Address comparison is case-insensitive; a checksummed intent against a
    lowercase tx is the same address."""
    d = _authorize(_intent(to=ESCROW.upper().replace("0X", "0x")))
    assert d.allowed, d.reason
