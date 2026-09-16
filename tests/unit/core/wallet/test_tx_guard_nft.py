"""tx_guard's FOURTH intent shape: a NON-FUNGIBLE move.

Three shapes came before it — a spend, a deploy (042), a claim. An NFT transfer
is none of them: `amount_raw` is 0 (nothing fungible leaves), `intent.token` is
None, and what moves is an IDENTIFIER, not an amount. Every existing branch
therefore refused it, exactly as they refused an approval before `is_allowance_op`
existed (the 2026-08-14 prod finding).

⚠️ The refusals here are the point, and they are NOT scoped to the new verbs.
Rules 6c/6d/6e run on EVERY transaction the guard sees, so `defi_trade.call`,
`dapp_connect` and any future caller-supplied calldata inherit them. Until
2026-09-15 a transaction could move a collectible out of the treasury and the
"undeclared token" refusal could not fire, because the event was never parsed.

⚠️ An NFT is UNPRICEABLE. Its cost here is the worst-case FEE and nothing else —
never $0.00, which is the confident-zero class that already bit this tree twice.
The caps cannot bound it, so the OWNER does: the verbs live in
`spend_lane.ALWAYS_OWNER_APPROVED_VERBS`, pinned by test_spend_lane_nft.py.
"""
import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NFT = "0x4444444444444444444444444444444444444444"
MARKET = "0x3333333333333333333333333333333333333333"
BUYER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
HOLDER = "0x2222222222222222222222222222222222222222"


def _gate():
    return PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0)


def _tx():
    return {"to": NFT, "data": "0x42842e0e", "value": 0, "chainId": 8453,
            "nonce": 5, "gas": 120_000, "maxFeePerGas": 10 ** 9}


def _authorize(intent, deltas, *, tx=None, gate=None, price=1.0):
    return tx_guard.authorize(
        intent, tx if tx is not None else _tx(),
        holder=HOLDER, gate=gate or _gate(), execution_context=None,
        simulate_fn=lambda **_: deltas,
        price_fn=lambda chain, addr: price,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False,
        entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False,
    )


def _nft_intent(**kw):
    base = dict(chain="base", token=None, to=NFT, amount_raw=0,
                max_spend_usd=5.0, idempotency_key="k", is_nft_op=True,
                nft_out=((NFT, "erc721", 42, 1),))
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _deltas(**kw):
    base = dict(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                gas_used=90_000,
                holder_nft_out=((NFT.lower(), "erc721", BUYER.lower(), 42, 1),))
    base.update(kw)
    return Deltas(**base)


# --- the shape is legal at all ---------------------------------------------

def test_a_declared_nft_move_that_the_simulation_confirms_is_authorized():
    d = _authorize(_nft_intent(), _deltas())
    assert d.allowed is True, d.reason


def test_zero_amount_raw_is_structurally_legal_for_an_nft_op():
    """The `is_allowance_op` precedent: a structural `amount > 0` rule made
    every approval dead on arrival on prod. Same class of bug."""
    d = _authorize(_nft_intent(), _deltas())
    assert "amount must be greater than zero" not in (d.reason or "")


def test_an_nft_move_is_priced_at_its_worst_case_fee_not_at_zero():
    """The cost of a transfer that sends no fungible is the FEE. Booking it at
    $0.00 is the confident-zero class that already bit this tree twice.

    Priced at realistic mainnet-ish parameters (50 gwei, $3000/ETH) so the
    figure survives the guard's 2-decimal rounding: worst-case gas is
    ``gas_used * 3/2`` = 135,000, so 135000 * 50 gwei = 0.00675 ETH = $20.25.
    """
    tx = dict(_tx(), maxFeePerGas=50 * 10 ** 9)
    d = _authorize(_nft_intent(max_spend_usd=50.0), _deltas(), tx=tx,
                   price=3000.0)
    assert d.allowed is True, d.reason
    assert d.amount_usd == pytest.approx(20.25)


def test_a_fee_above_the_declared_ceiling_refuses():
    """The fee is not decoration — it is held to the declaration like any
    other cost, so a gas spike cannot quietly outrun what was authorized."""
    tx = dict(_tx(), maxFeePerGas=50 * 10 ** 9)
    d = _authorize(_nft_intent(max_spend_usd=5.0), _deltas(), tx=tx,
                   price=3000.0)
    assert d.allowed is False
    assert "exceeds the declared max_spend_usd" in d.reason


# --- 6c: an UNDECLARED outflow refuses (the whole point) -------------------

def test_an_undeclared_nft_outflow_refuses():
    """The transaction moves a collectible the intent never mentioned."""
    d = _authorize(
        tx_guard.TxIntent(chain="base", token=USDC, to=BUYER,
                          amount_raw=1_000_000, max_spend_usd=5.0,
                          idempotency_key="k"),
        _deltas(token_deltas={USDC: -1_000_000}))
    assert d.allowed is False
    assert "UNDECLARED" in d.reason and "42" in d.reason


def test_an_undeclared_nft_outflow_refuses_on_an_arbitrary_call():
    """Rule 6c is not scoped to the NFT verbs — `defi_trade.call` inherits it.

    A call that spends exactly what it declared and returns what it promised
    still refuses if a collectible left alongside."""
    d = _authorize(
        tx_guard.TxIntent(chain="base", token=USDC, to=MARKET,
                          amount_raw=1_000_000, max_spend_usd=5.0,
                          idempotency_key="k"),
        _deltas(token_deltas={USDC: -1_000_000},
                holder_transfers=((USDC.lower(), MARKET.lower(), 1_000_000),)))
    assert d.allowed is False
    assert "UNDECLARED non-fungible" in d.reason


def test_a_move_of_a_DIFFERENT_token_id_than_declared_refuses():
    d = _authorize(
        _nft_intent(nft_out=((NFT, "erc721", 42, 1),)),
        _deltas(holder_nft_out=((NFT.lower(), "erc721", BUYER.lower(), 99, 1),)))
    assert d.allowed is False
    assert "UNDECLARED" in d.reason


def test_a_move_of_a_DIFFERENT_contract_than_declared_refuses():
    d = _authorize(
        _nft_intent(),
        _deltas(holder_nft_out=((MARKET.lower(), "erc721", BUYER.lower(), 42, 1),)))
    assert d.allowed is False
    assert "UNDECLARED" in d.reason


def test_an_erc1155_quantity_above_the_declared_amount_refuses():
    d = _authorize(
        _nft_intent(nft_out=((NFT, "erc1155", 7, 2),)),
        _deltas(holder_nft_out=((NFT.lower(), "erc1155", BUYER.lower(), 7, 5),)))
    assert d.allowed is False


# --- 6d: a DECLARED move must actually be measured ------------------------

def test_a_declared_nft_move_the_simulation_does_not_emit_refuses():
    """Broadcasting on a promise is what the 042 min-inflow assertion ended for
    fungibles. The same rule, for the shape that carries no amount at all."""
    d = _authorize(_nft_intent(), _deltas(holder_nft_out=()))
    assert d.allowed is False
    assert "does not move" in d.reason


def test_a_declared_nft_inflow_that_never_arrives_refuses():
    d = _authorize(
        _nft_intent(nft_out=(), expected_nft_in=((NFT, "erc721", 7, 1),)),
        _deltas(holder_nft_out=(), holder_nft_in=()))
    assert d.allowed is False
    assert "does not arrive" in d.reason


def test_a_declared_nft_inflow_that_arrives_is_authorized():
    d = _authorize(
        _nft_intent(nft_out=(), expected_nft_in=((NFT, "erc721", 7, 1),)),
        _deltas(holder_nft_out=(),
                holder_nft_in=((NFT.lower(), "erc721", MARKET.lower(), 7, 1),)))
    assert d.allowed is True, d.reason


# --- 6e: the blanket grant, which no verb may ever author ------------------

def test_an_undeclared_approval_for_all_refuses():
    d = _authorize(
        _nft_intent(),
        _deltas(holder_operator_grants=((NFT.lower(), MARKET.lower(), True),)))
    assert d.allowed is False
    assert "ApprovalForAll" in d.reason


def test_an_approval_for_all_grant_refuses_even_when_declared():
    """⚠️ A blanket grant is a standing claim on every token of a collection,
    present AND future, and no simulation can bound what it later enables. No
    verb in this tree may author one, so declaring it must not buy it through."""
    d = _authorize(
        _nft_intent(nft_operator_ops=((NFT, MARKET, True),)),
        _deltas(holder_operator_grants=((NFT.lower(), MARKET.lower(), True),)))
    assert d.allowed is False
    assert "ApprovalForAll" in d.reason


def test_a_declared_revoke_to_false_is_authorized():
    """Setting an operator grant to False can only RETIRE a claim."""
    d = _authorize(
        _nft_intent(nft_out=(), nft_operator_ops=((NFT, MARKET, False),)),
        _deltas(holder_nft_out=(),
                holder_operator_grants=((NFT.lower(), MARKET.lower(), False),)))
    assert d.allowed is True, d.reason


def test_an_undeclared_revoke_is_allowed_because_it_only_reduces_risk():
    d = _authorize(
        _nft_intent(),
        _deltas(holder_operator_grants=((NFT.lower(), MARKET.lower(), False),)))
    assert d.allowed is True, d.reason


def test_an_undeclared_single_token_erc721_approval_refuses():
    d = _authorize(
        _nft_intent(),
        _deltas(holder_nft_approvals=((NFT.lower(), MARKET.lower(), 42),)))
    assert d.allowed is False
    assert "Approval" in d.reason


# --- nothing that worked before may change ---------------------------------

def test_an_ordinary_erc20_transfer_is_unaffected():
    d = _authorize(
        tx_guard.TxIntent(chain="base", token=USDC, to=BUYER,
                          amount_raw=1_000_000, max_spend_usd=5.0,
                          idempotency_key="k"),
        Deltas(ok=True, native_delta=0, token_deltas={USDC: -1_000_000},
               allowance_deltas={}, gas_used=90_000,
               holder_transfers=((USDC.lower(), BUYER.lower(), 1_000_000),)))
    assert d.allowed is True, d.reason


def test_an_nft_op_cannot_also_be_a_deploy():
    d = _authorize(_nft_intent(is_deploy=True, to=None), _deltas())
    assert d.allowed is False


def test_an_nft_op_must_declare_something():
    """An NFT op that declares no move and no receipt asserts nothing at all —
    it is an arbitrary call to a contract wearing an NFT verb's name."""
    d = _authorize(_nft_intent(nft_out=(), expected_nft_in=()),
                   _deltas(holder_nft_out=()))
    assert d.allowed is False
    assert "declare" in d.reason
