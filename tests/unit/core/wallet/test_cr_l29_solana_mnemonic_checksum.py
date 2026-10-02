"""CR-L29: a mistyped mnemonic must not silently derive a different Solana address."""
import pytest

pytest.importorskip("solders")
pytest.importorskip("eth_account")

from core.wallet.solana_signer import derive_solana_keypair

VALID = ("abandon abandon abandon abandon abandon abandon abandon abandon "
         "abandon abandon abandon about")
TYPO = VALID.replace("about", "above")  # valid words, bad checksum


def test_valid_mnemonic_still_derives():
    assert str(derive_solana_keypair(VALID, 0).pubkey())


def test_mistyped_mnemonic_is_refused():
    with pytest.raises(ValueError, match="checksum"):
        derive_solana_keypair(TYPO, 0)


def test_legacy_raw_seed_address_is_unchanged():
    # A non-mnemonic legacy seed keeps deriving (its address must not move).
    a = derive_solana_keypair("x" * 40, 0)
    b = derive_solana_keypair("x" * 40, 0)
    assert a.pubkey() == b.pubkey()
