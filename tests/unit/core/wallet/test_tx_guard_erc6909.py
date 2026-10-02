"""C17 (polyrob-desk handoff-core) — ERC-6909 (Uniswap v4 claims) in the guard.

An ERC-6909 allowance or operator survives a sale like any approval, and a claim token moves
without an ERC-20 or ERC-721 event. The guard: refuses any ERC-6909 grant (none can be
declared), measures an ERC-6909 move like an NFT's (undeclared = refused), and holds a declared
revoke to its zero ``Approval``.
"""
from core.wallet import tx_guard
from core.wallet.simulation import Deltas
from tests.unit.core.wallet.test_tx_guard_via_account import (
    ACCOUNT, NFT, TO, _erc20_deltas, _erc20_inner, _erc20_intent, _pins, _run)

assert _pins  # the code-pin fixture (autouse)

SPENDER = "0x9999999999999999999999999999999999999999"


def _direct(inner):
    return dict(inner, chainId=8453, nonce=5, gas=200_000, maxFeePerGas=10 ** 8)


def _revoke_intent(**kw):
    base = dict(chain="base", token=None, to=NFT, amount_raw=0, max_spend_usd=1.0,
                idempotency_key="r", is_nft_op=True, erc6909_revokes=((NFT, SPENDER, 7),))
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _revoke_tx():
    from core.wallet import erc6551
    return _direct({"to": NFT, "value": 0, "data": erc6551.encode_erc6909_revoke(SPENDER, 7)})


def test_a_declared_revoke_with_its_zero_approval_is_allowed():
    deltas = Deltas(ok=True, gas_used=50_000,
                    holder_6909_approvals=((NFT.lower(), SPENDER.lower(), 7, 0),))
    d = _run(_revoke_intent(), deltas, _revoke_tx(), holder=ACCOUNT)
    assert d.allowed is True, d.reason


def test_a_declared_revoke_the_simulation_does_not_show_refuses():
    d = _run(_revoke_intent(), Deltas(ok=True, gas_used=50_000), _revoke_tx(), holder=ACCOUNT)
    assert d.allowed is False and "ERC-6909 allowance" in d.reason


def test_a_revoke_must_be_an_nft_op():
    d = _run(_revoke_intent(is_nft_op=False, amount_raw=1), Deltas(ok=True, gas_used=1),
             _revoke_tx(), holder=ACCOUNT)
    assert d.allowed is False and "is_nft_op" in d.reason, d.reason


def test_any_6909_grant_refuses():
    deltas = _erc20_deltas(holder_6909_approvals=((NFT.lower(), SPENDER.lower(), 7, 1),))
    d = _run(_erc20_intent(), deltas, _direct(_erc20_inner()), holder=ACCOUNT)
    assert d.allowed is False and "ERC-6909 Approval" in d.reason


def test_a_6909_operator_grant_refuses_as_a_blanket_grant():
    deltas = _erc20_deltas(holder_operator_grants=((NFT.lower(), SPENDER.lower(), True),))
    d = _run(_erc20_intent(), deltas, _direct(_erc20_inner()), holder=ACCOUNT)
    assert d.allowed is False and "blanket operator approval" in d.reason


def test_an_undeclared_6909_move_refuses():
    deltas = _erc20_deltas(holder_nft_out=((NFT.lower(), "erc6909", TO.lower(), 7, 500),))
    d = _run(_erc20_intent(), deltas, _direct(_erc20_inner()), holder=ACCOUNT)
    assert d.allowed is False and "erc6909" in d.reason
