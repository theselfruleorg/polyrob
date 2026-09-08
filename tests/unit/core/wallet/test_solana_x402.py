"""Solana x402 — Phase 4. The pay-side signer adapter.

The x402 SDK ships a full SVM `exact` mechanism (2.16.0:
`x402/mechanisms/svm/exact/`), so this phase is NOT blocked on the spec, as an
earlier note claimed. What it needs from us is a `ClientSvmSigner`, and that
protocol wants the raw `Keypair` — which `SolanaSigner` deliberately does not
hand out.

So the adapter is the seam: it satisfies the SDK in-process and keeps the
signer's perimeter intact everywhere else. Same precedent as
`LocalEoaSigner.account`, which exists for exactly this reason (the Hyperliquid
SDK) with the same in-process-only caveat.
"""
import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from core.wallet.solana_signer import SolanaSigner
from core.wallet.solana_x402 import SolanaX402Signer

MNEMONIC = ("abandon abandon abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon about")


def _adapter():
    return SolanaX402Signer(SolanaSigner.from_mnemonic(MNEMONIC))


def test_it_satisfies_the_sdk_protocol_shape():
    a = _adapter()
    assert isinstance(a.address, str) and not a.address.startswith("0x")
    assert hasattr(a, "keypair")
    assert callable(a.sign_transaction)


def test_the_address_matches_the_underlying_signer():
    s = SolanaSigner.from_mnemonic(MNEMONIC)
    assert SolanaX402Signer(s).address == s.address


def test_the_keypair_is_the_real_one_the_sdk_needs():
    from solders.keypair import Keypair
    assert isinstance(_adapter().keypair, Keypair)


def test_the_base_signer_still_does_not_expose_a_keypair():
    """The perimeter holds where it matters. The adapter is the ONLY place the
    keypair is reachable, and it exists for one SDK."""
    s = SolanaSigner.from_mnemonic(MNEMONIC)
    assert not hasattr(s, "_SolanaSigner__keypair_public")
    # the private attribute is name-mangled, not a public surface
    assert "keypair" not in [a for a in dir(s) if not a.startswith("_")]


def test_the_adapter_repr_never_leaks_the_key():
    a = _adapter()
    text = repr(a)
    assert a.address in text
    secret = bytes(a.keypair)[:32]
    assert secret.hex() not in text


def test_signing_refuses_a_transaction_we_are_not_part_of():
    """The adapter must not weaken the perimeter it exists to work around.

    Note the rule is subtler than the base signer's: an x402 SVM payment is
    fee-paid by the FACILITATOR, so "payer must be us" would refuse every
    legitimate payment. What must always hold is that we never sign for an
    account set we do not appear in at all."""
    import solders
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    from core.wallet.solana_signer import derive_solana_keypair
    a = _adapter()
    other = derive_solana_keypair(MNEMONIC, 9).pubkey()
    msg = MessageV0.try_compile(other, [], [], Hash.default())
    tx = VersionedTransaction.populate(msg, [solders.signature.Signature.default()])
    with pytest.raises(ValueError, match="does not involve|fee payer"):
        a.sign_transaction(tx)


def test_signing_is_allowed_when_a_facilitator_pays_and_we_are_the_authority():
    """The case the base signer's rule would wrongly refuse. We are in the
    account set as the token authority; the facilitator pays the fee."""
    import solders
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    from core.wallet.solana_signer import derive_solana_keypair
    a = _adapter()
    facilitator = derive_solana_keypair(MNEMONIC, 9).pubkey()
    me = Pubkey.from_string(a.address)
    ix = Instruction(Pubkey.default(), b"", [AccountMeta(me, True, False)])
    msg = MessageV0.try_compile(facilitator, [ix], [], Hash.default())
    tx = VersionedTransaction.populate(
        msg, [solders.signature.Signature.default()] * 2)
    signed = a.sign_transaction(tx)          # must NOT raise
    # our slot is filled, the facilitator's is untouched
    assert signed.signatures[1] != solders.signature.Signature.default()
    assert signed.signatures[0] == solders.signature.Signature.default()
    # and our signature is genuinely valid over this message
    assert signed.signatures[1].verify(me, bytes(signed.message))


def test_signing_refuses_a_slot_that_is_not_a_required_signer():
    """Present in the account set but not as a signer. Writing into someone
    else's slot would corrupt the payload."""
    import solders
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    from core.wallet.solana_signer import derive_solana_keypair
    a = _adapter()
    facilitator = derive_solana_keypair(MNEMONIC, 9).pubkey()
    me = Pubkey.from_string(a.address)
    ix = Instruction(Pubkey.default(), b"", [AccountMeta(me, False, True)])
    msg = MessageV0.try_compile(facilitator, [ix], [], Hash.default())
    tx = VersionedTransaction.populate(msg, [solders.signature.Signature.default()])
    with pytest.raises(ValueError, match="required signer"):
        a.sign_transaction(tx)


def test_the_x402_network_name_maps_from_our_registry():
    from core.wallet.solana_x402 import x402_network
    assert x402_network("mainnet") == "solana"
    assert x402_network("testnet") == "solana-devnet"


def test_the_usdc_mint_is_network_correct():
    from core.wallet.solana_x402 import usdc_mint
    assert usdc_mint("mainnet") == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert usdc_mint("testnet") != usdc_mint("mainnet")
