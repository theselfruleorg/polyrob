"""Solana keys and signing — Phase 2 (no money moves).

Phantom-parity derivation (`m/44'/501'/0'/0'`, SLIP-0010 ed25519) so the SAME
mnemonic the EVM side uses imports into a normal Solana wallet. Two invariants
carry the weight here:

* **The EVM address must not move.** Derivation is additive; adding a Solana
  branch may not perturb `m/44'/60'/…`.
* **The key never leaves the signer.** Same perimeter rule as `LocalEoaSigner`.
"""
import pytest

solders = pytest.importorskip("solders", reason="needs the `solana` extra")

from core.wallet.solana_signer import (SolanaSigner, derive_solana_keypair,
                                       solana_derivation_path)

# BIP-39 test vector. Phantom derives the address below from it at m/44'/501'/0'/0'.
MNEMONIC = ("abandon abandon abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon about")


def test_the_derivation_path_is_phantoms():
    """Wallet-import parity: the owner must be able to open this account in a
    normal Solana wallet with the same seed phrase."""
    assert solana_derivation_path(0) == "m/44'/501'/0'/0'"
    assert solana_derivation_path(1) == "m/44'/501'/1'/0'"


def test_derivation_is_deterministic():
    a = derive_solana_keypair(MNEMONIC, 0)
    b = derive_solana_keypair(MNEMONIC, 0)
    assert str(a.pubkey()) == str(b.pubkey())


def test_a_different_account_index_is_a_different_key():
    a = derive_solana_keypair(MNEMONIC, 0)
    b = derive_solana_keypair(MNEMONIC, 1)
    assert str(a.pubkey()) != str(b.pubkey())


def test_the_address_is_a_valid_base58_account_key():
    from core.wallet.addresses import normalize_for_chain
    addr = str(derive_solana_keypair(MNEMONIC, 0).pubkey())
    assert normalize_for_chain("solana", addr) == addr


def test_adding_solana_does_not_move_the_evm_address():
    """The addresses-never-change invariant. A funded EVM wallet must not shift
    because a Solana branch was added."""
    from core.wallet.derivation import derive_key
    from eth_account import Account
    before = Account.from_key(derive_key(MNEMONIC, "treasury", "bip44")).address
    derive_solana_keypair(MNEMONIC, 0)          # touch the new path
    after = Account.from_key(derive_key(MNEMONIC, "treasury", "bip44")).address
    assert before == after


# -- the signer perimeter ----------------------------------------------------

def test_the_signer_exposes_its_address_and_nothing_else():
    s = SolanaSigner.from_mnemonic(MNEMONIC)
    assert s.address == str(derive_solana_keypair(MNEMONIC, 0).pubkey())
    for attr in ("private_key", "secret", "seed", "keypair_bytes"):
        assert not hasattr(s, attr), attr


def test_the_repr_never_leaks_the_key():
    s = SolanaSigner.from_mnemonic(MNEMONIC)
    text = repr(s)
    assert s.address in text
    assert "Keypair" not in text
    secret = bytes(derive_solana_keypair(MNEMONIC, 0))[:32]
    assert secret.hex() not in text


def test_it_signs_a_message_verifiably():
    from solders.pubkey import Pubkey
    s = SolanaSigner.from_mnemonic(MNEMONIC)
    sig = s.sign_message(b"polyrob")
    # A real ed25519 signature over exactly these bytes.
    assert sig.verify(Pubkey.from_string(s.address), b"polyrob")


def test_a_signature_does_not_verify_for_other_bytes():
    from solders.pubkey import Pubkey
    s = SolanaSigner.from_mnemonic(MNEMONIC)
    sig = s.sign_message(b"polyrob")
    assert not sig.verify(Pubkey.from_string(s.address), b"tampered")


def test_signing_an_unsigned_versioned_transaction_yields_a_signed_one():
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    s = SolanaSigner.from_mnemonic(MNEMONIC)
    me = Pubkey.from_string(s.address)
    msg = MessageV0.try_compile(me, [], [], Hash.default())
    unsigned = VersionedTransaction.populate(msg, [solders.signature.Signature.default()])
    signed = s.sign_transaction(unsigned)
    assert signed.signatures[0] != solders.signature.Signature.default()
    # solders verifies against the wire-serialised message itself; asking it is
    # more honest than re-deriving the signed bytes here and guessing wrong.
    assert all(signed.verify_with_results()), signed.verify_with_results()


def test_refusing_to_sign_a_transaction_that_is_not_ours():
    """The fee payer must be this signer. A transaction whose first required
    signer is somebody else is not ours to sign, and signing it blind is how an
    aggregator-authored payload gets a signature it should never have had."""
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    s = SolanaSigner.from_mnemonic(MNEMONIC)
    other = Pubkey.from_string(str(derive_solana_keypair(MNEMONIC, 7).pubkey()))
    msg = MessageV0.try_compile(other, [], [], Hash.default())
    unsigned = VersionedTransaction.populate(msg, [solders.signature.Signature.default()])
    with pytest.raises(ValueError, match="fee payer"):
        s.sign_transaction(unsigned)


# --------------------------------------------------------------------------
# Wiring into the agent wallet — one seed, two families
# --------------------------------------------------------------------------

def _wallet(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", MNEMONIC)
    monkeypatch.setenv("AGENT_WALLET_DERIVATION", "bip44")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from core.wallet.agent_wallet import AgentWallet
    from core.wallet.config import load_wallet_config
    return AgentWallet(load_wallet_config())


def test_the_wallet_exposes_a_solana_address(monkeypatch, tmp_path):
    w = _wallet(monkeypatch, tmp_path)
    assert w.solana_address == str(derive_solana_keypair(MNEMONIC, 0).pubkey())


def test_the_solana_address_is_not_the_evm_address(monkeypatch, tmp_path):
    """Different curve, different account. The owner must fund BOTH, and a UI
    that showed one for the other would strand funds."""
    w = _wallet(monkeypatch, tmp_path)
    assert w.solana_address != w.address
    assert not w.solana_address.startswith("0x")


def test_the_solana_signer_is_cached_like_the_evm_ones(monkeypatch, tmp_path):
    w = _wallet(monkeypatch, tmp_path)
    assert w.solana_signer() is w.solana_signer()


def test_the_evm_address_is_unchanged_by_the_solana_branch(monkeypatch, tmp_path):
    """Pinned twice over: the addresses-never-change invariant is the one thing
    that cannot be walked back once an address is funded."""
    w = _wallet(monkeypatch, tmp_path)
    evm_before = w.address
    _ = w.solana_address
    assert w.address == evm_before
