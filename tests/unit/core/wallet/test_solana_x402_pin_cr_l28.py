"""CR-L28: SolanaX402Signer signs only the pinned SPL TransferChecked
(plus ComputeBudget and the SDK's account-less Memo)."""
import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

import solders
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.wallet.solana_signer import SolanaSigner, derive_solana_keypair
from core.wallet.solana_x402 import SolanaX402Signer

from tests.unit.core.wallet.test_solana_x402 import (
    MNEMONIC, _MINT, _PAY_TO, _transfer_checked)

CB = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
MEMO = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")


def _signer(pinned=True):
    a = SolanaX402Signer(SolanaSigner.from_mnemonic(MNEMONIC))
    if pinned:
        a.pin(mint=_MINT, pay_to=_PAY_TO, amount=1_000)
    return a


def _tx(a, ixs):
    facilitator = derive_solana_keypair(MNEMONIC, 9).pubkey()
    msg = MessageV0.try_compile(facilitator, ixs, [], Hash.default())
    n = msg.header.num_required_signatures
    return VersionedTransaction.populate(msg, [solders.signature.Signature.default()] * n)


def _sdk_shape(a, **kw):
    return [Instruction(CB, bytes([2]) + (200_000).to_bytes(4, "little"), []),
            Instruction(CB, bytes([3]) + (1).to_bytes(8, "little"), []),
            _transfer_checked(a.address, **kw),
            Instruction(MEMO, b"deadbeef", [])]


def test_the_sdk_payment_shape_signs():
    a = _signer()
    signed = a.sign_transaction(_tx(a, _sdk_shape(a)))
    me = Pubkey.from_string(a.address)
    assert signed.signatures[1].verify(me, bytes(signed.message))


def test_an_unpinned_signer_signs_nothing():
    a = _signer(pinned=False)
    with pytest.raises(ValueError, match="no payment is pinned"):
        a.sign_transaction(_tx(a, _sdk_shape(a)))


@pytest.mark.parametrize("kw", [
    {"amount": 1_001},
    {"pay_to": "11111111111111111111111111111112"},
    {"mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},
])
def test_a_transfer_that_differs_from_the_pin_is_refused(kw):
    a = _signer()
    with pytest.raises(ValueError, match="pinned payment"):
        a.sign_transaction(_tx(a, _sdk_shape(a, **kw)))


def test_an_extra_instruction_is_refused():
    a = _signer()
    me = Pubkey.from_string(a.address)
    extra = Instruction(Pubkey.from_string("11111111111111111111111111111111"),
                        bytes(12), [AccountMeta(me, True, True)])
    with pytest.raises(ValueError, match="unexpected program"):
        a.sign_transaction(_tx(a, _sdk_shape(a) + [extra]))


def test_two_transfers_are_refused():
    a = _signer()
    with pytest.raises(ValueError, match="exactly one transfer"):
        a.sign_transaction(_tx(a, _sdk_shape(a) + [_transfer_checked(a.address)]))


def test_a_memo_carrying_accounts_is_refused():
    a = _signer()
    me = Pubkey.from_string(a.address)
    memo = Instruction(MEMO, b"x", [AccountMeta(me, True, False)])
    with pytest.raises(ValueError):
        a.sign_transaction(_tx(a, [_transfer_checked(a.address), memo]))
