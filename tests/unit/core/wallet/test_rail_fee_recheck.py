"""Audit 2026-11-21 L-6: the attestation digest binds what a transaction DOES
(chainId, to, value, data, nonce) and not its gas, fee fields or ``type`` —
``size_gas`` rewrites ``gas`` after ``tx_guard`` authorized it. So the rail
re-checks the fee bound itself at the signing point: ``gas * maxFeePerGas``
(and ``gasPrice``) must stay within the chain's max fee per transaction, and
only an EIP-1559 (type 2) transaction is signed.
"""
import pytest
from eth_utils import keccak

from core.signer import attest
from tests.unit.core.signer.conftest import native_tx, signer_home  # noqa: F401 — fixture


class _Signer:
    address = "0x" + "1" * 40

    def __init__(self):
        self.signed = []

    def sign_transaction(self, tx):
        self.signed.append(dict(tx))
        return b"synthetic signed bytes"


def _rail(signer, rpc_log):
    from core.wallet.broadcast.evm import EvmRail
    rail = EvmRail("base", signer)

    def rpc(method, params, timeout=8.0):
        rpc_log.append(method)
        if method == "eth_chainId":
            return hex(8453)
        if method == "eth_sendRawTransaction":
            return "0x" + keccak(b"synthetic signed bytes").hex()
        raise AssertionError(method)
    rail._rpc = rpc
    return rail


def _authorized(tx):
    """Record an authorization for *tx* as tx_guard would (digest without fees)."""
    attest.record(object(), tx)
    assert attest.peek(tx) is not None
    return tx


def _refused(tx, match):
    from core.wallet.broadcast.evm import BroadcastError
    signer, rpc = _Signer(), []
    rail = _rail(signer, rpc)
    with pytest.raises(BroadcastError, match=match):
        rail.sign_and_send(tx)
    assert signer.signed == [] and rpc == []
    return rail


def test_a_fee_raised_after_authorization_is_refused_before_signing(signer_home):
    tx = _authorized(native_tx())
    rail = _rail(_Signer(), [])
    # Same digest (fees are not bound), but gas x maxFeePerGas over the ceiling.
    tx["maxFeePerGas"] = rail._max_fee_wei // tx["gas"] + 1
    assert attest.peek(tx) is not None
    _refused(tx, "max fee")


def test_a_gas_limit_raised_after_authorization_is_refused(signer_home):
    tx = _authorized(native_tx())
    rail = _rail(_Signer(), [])
    tx["gas"] = rail._max_fee_wei // tx["maxFeePerGas"] + 1
    _refused(tx, "max fee")


def test_a_legacy_gas_price_counts_toward_the_fee(signer_home):
    tx = _authorized(native_tx())
    rail = _rail(_Signer(), [])
    tx["gasPrice"] = rail._max_fee_wei // tx["gas"] + 1
    _refused(tx, "max fee|type")


@pytest.mark.parametrize("tx_type", [0, 1, 3, 4, "0x0"])
def test_a_transaction_type_other_than_eip1559_is_refused(signer_home, tx_type):
    tx = _authorized(native_tx(type=tx_type))
    _refused(tx, "type")


def test_a_legacy_shape_without_a_type_is_refused(signer_home):
    tx = native_tx()
    tx.pop("type")
    tx.pop("maxFeePerGas")
    tx.pop("maxPriorityFeePerGas")
    tx["gasPrice"] = 10 ** 9
    _refused(_authorized(tx), "type")


def test_a_missing_gas_limit_is_refused(signer_home):
    tx = native_tx()
    tx.pop("gas")
    _refused(_authorized(tx), "gas")


def test_a_fee_within_the_ceiling_still_sends(signer_home):
    tx = _authorized(native_tx())
    signer, rpc = _Signer(), []
    assert _rail(signer, rpc).sign_and_send(tx)
    assert len(signer.signed) == 1 and "eth_sendRawTransaction" in rpc
