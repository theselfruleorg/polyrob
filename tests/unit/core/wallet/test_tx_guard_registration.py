"""tx_guard's SIXTH intent shape: registering an agent identity.

After a spend, an allowance, a deploy (042), a claim and a non-fungible move
(2026-09-15). Like every one of those, it exists because every other branch
refused it for a different reason: `amount_raw == 0`, `token=None`, no
counterparty to assert against — and, most importantly, **nothing at all**
checking that the registry actually minted us anything.

⚠️ THE RECEIPT ASSERTION IS THE POINT. `register()` sends no value, so without
it the guard cannot tell "I registered" from "I called a contract that took my
gas". This is the same property the 042 deploy design insisted on (does the
CREATE produce code at all?) and the 2026-09-14 claim shape insisted on (does
the claim actually pay?).

⚠️ THE DESTINATION IS PINNED, NOT DECLARED. `expected_registry` must come from
`core.wallet.erc8004`. A caller-supplied destination would make this verb a
generic "call an arbitrary contract from the treasury wallet" wearing a
registration's name.
"""
import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
OTHER = "0x3333333333333333333333333333333333333333"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
HOLDER = "0x2222222222222222222222222222222222222222"
ZERO = "0x0000000000000000000000000000000000000000"


def _tx(to=REGISTRY):
    return {"to": to, "data": "0x1aa3a008", "value": 0, "chainId": 8453,
            "nonce": 5, "gas": 400_000, "maxFeePerGas": 10 ** 9}


def _authorize(intent, deltas, *, tx=None, price=3000.0):
    return tx_guard.authorize(
        intent, tx if tx is not None else _tx(),
        holder=HOLDER, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: deltas,
        price_fn=lambda chain, addr: price,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False)


def _intent(**kw):
    base = dict(chain="base", token=None, to=REGISTRY, amount_raw=0,
                max_spend_usd=50.0, idempotency_key="k",
                is_registration=True, expected_registry=REGISTRY)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _minted(contract=REGISTRY, frm=ZERO, token_id=42):
    """The Registered event's ERC-721 side: a mint to us."""
    return ((contract.lower(), "erc721", frm.lower(), token_id, 1),)


def _deltas(**kw):
    base = dict(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                gas_used=300_000, holder_nft_in=_minted())
    base.update(kw)
    return Deltas(**base)


# --- the happy path -------------------------------------------------------

def test_a_registration_that_mints_us_a_token_is_authorized():
    d = _authorize(_intent(), _deltas())
    assert d.allowed is True, d.reason


def test_zero_amount_raw_is_structurally_legal():
    """The `is_allowance_op` precedent: a blanket `amount > 0` rule made every
    approval dead on arrival on prod (2026-08-14). Same class."""
    d = _authorize(_intent(), _deltas())
    assert "amount must be greater than zero" not in (d.reason or "")


def test_the_cost_is_the_fee_and_is_never_zero_dollars():
    """A registration sends nothing, so pricing an outflow would book it at
    $0.00 — the confident-zero class already twice on record here."""
    tx = dict(_tx(), maxFeePerGas=50 * 10 ** 9)
    # Worst-case gas is gas_used * 3/2 = 450,000; at 50 gwei and $3000/ETH that
    # is $67.50, so the declared ceiling has to accommodate a real L1 fee.
    d = _authorize(_intent(max_spend_usd=100.0), _deltas(), tx=tx)
    assert d.amount_usd == pytest.approx(67.5)


# --- the receipt assertion ------------------------------------------------

def test_a_registration_that_mints_NOTHING_refuses():
    """⚠️ Without this the guard cannot tell a registration from a contract call
    that merely consumed gas."""
    d = _authorize(_intent(), _deltas(holder_nft_in=()))
    assert d.allowed is False
    assert "does not mint" in d.reason or "no agent token" in d.reason


def test_a_mint_from_a_DIFFERENT_contract_refuses():
    """Something minted us a token, but not the registry we asked for."""
    d = _authorize(_intent(), _deltas(holder_nft_in=_minted(contract=OTHER)))
    assert d.allowed is False


def test_a_transfer_that_is_not_a_MINT_refuses():
    """⚠️ A mint comes from the zero address. A plain transfer of an existing
    token to us is somebody handing over THEIR identity, not us registering."""
    d = _authorize(_intent(), _deltas(holder_nft_in=_minted(frm=OTHER)))
    assert d.allowed is False
    assert "mint" in d.reason.lower()


def test_the_minted_token_id_is_reported_back():
    """The agentId IS the tokenId, and the caller needs it — reading it off the
    simulation is how the verb knows what it got without a second lookup."""
    d = _authorize(_intent(), _deltas(holder_nft_in=_minted(token_id=77)))
    assert d.allowed is True, d.reason
    assert d.agent_id == 77


# --- the destination is pinned --------------------------------------------

def test_a_destination_that_is_not_the_declared_registry_refuses():
    d = _authorize(_intent(), _deltas(), tx=_tx(to=OTHER))
    assert d.allowed is False
    assert "registry" in d.reason.lower()


def test_a_registration_with_no_expected_registry_refuses():
    """Declaring nothing asserts nothing — it is an arbitrary contract call."""
    d = _authorize(_intent(expected_registry=None), _deltas())
    assert d.allowed is False
    assert "registry" in d.reason.lower()


# --- nothing may leave ----------------------------------------------------

def test_a_registration_that_also_moves_a_token_refuses():
    d = _authorize(_intent(), _deltas(
        holder_transfers=((USDC.lower(), OTHER.lower(), 1_000_000),)))
    assert d.allowed is False
    assert "UNDECLARED" in d.reason


def test_a_registration_that_also_sends_an_nft_refuses():
    d = _authorize(_intent(), _deltas(
        holder_nft_out=((USDC.lower(), "erc721", OTHER.lower(), 1, 1),)))
    assert d.allowed is False


def test_a_registration_that_grants_approval_for_all_refuses():
    d = _authorize(_intent(), _deltas(
        holder_operator_grants=((REGISTRY.lower(), OTHER.lower(), True),)))
    assert d.allowed is False
    assert "ApprovalForAll" in d.reason


def test_a_registration_that_drains_native_value_refuses():
    d = _authorize(_intent(), _deltas(native_delta=-10 ** 17))
    assert d.allowed is False


# --- shape exclusivity ----------------------------------------------------

@pytest.mark.parametrize("other", ["is_deploy", "is_claim", "is_nft_op"])
def test_a_registration_cannot_also_be_another_shape(other):
    d = _authorize(_intent(**{other: True}), _deltas())
    assert d.allowed is False


# --- everything else is untouched ------------------------------------------

def test_an_ordinary_transfer_is_unaffected():
    d = _authorize(
        tx_guard.TxIntent(chain="base", token=USDC, to=OTHER,
                          amount_raw=1_000_000, max_spend_usd=5.0,
                          idempotency_key="k"),
        Deltas(ok=True, native_delta=0, token_deltas={USDC: -1_000_000},
               allowance_deltas={}, gas_used=90_000,
               holder_transfers=((USDC.lower(), OTHER.lower(), 1_000_000),)),
        price=1.0)
    assert d.allowed is True, d.reason


# --- the UPDATE half: a registration that must mint NOTHING ----------------
#
# ⚠️ `setAgentURI` changes the published file and mints nothing. The first
# version of this verb smuggled it through as an `is_nft_op` declaring an
# ApprovalForAll revoke that never happens — a declaration the guard could not
# falsify, so it passed vacuously. `expects_mint=False` makes the assertion the
# exact INVERSE of rule 6f, which catches the failure that actually matters: an
# "update" that mints is a SECOND identity.

def test_an_update_that_mints_nothing_is_authorized():
    d = _authorize(_intent(expects_mint=False), _deltas(holder_nft_in=()))
    assert d.allowed is True, d.reason


def test_an_update_that_MINTS_refuses():
    """⚠️ The real hazard. Silently minting on an update leaves two agentIds."""
    d = _authorize(_intent(expects_mint=False), _deltas())
    assert d.allowed is False
    assert "mint" in d.reason.lower()


def test_an_update_still_may_not_move_anything():
    d = _authorize(_intent(expects_mint=False),
                   _deltas(holder_nft_in=(), native_delta=-10 ** 17))
    assert d.allowed is False


def test_an_update_still_must_target_the_pinned_registry():
    d = _authorize(_intent(expects_mint=False), _deltas(holder_nft_in=()),
                   tx=_tx(to=OTHER))
    assert d.allowed is False


def test_an_update_reports_no_agent_id():
    """Nothing was minted, so there is no id to report — reporting one would be
    inventing a fact."""
    d = _authorize(_intent(expects_mint=False), _deltas(holder_nft_in=()))
    assert d.agent_id is None
