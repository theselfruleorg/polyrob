"""Family-dispatched address validation (029 / Solana Phase 0).

`normalize_address` is EIP-55. EIP-55 is a checksum, and refusing a failed one
is the only typo detector Ethereum has. Base58 has NO checksum, so the Solana
side of this seam cannot offer the same protection and must not pretend to —
these tests pin what each family actually guarantees, and pin that neither one
ever accepts the other's format.
"""
import pytest

from core.wallet.addresses import (family_of, normalize_for_chain,
                                   typo_protection_note)

EVM = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_WSOL = "So11111111111111111111111111111111111111112"


def test_the_family_comes_from_the_registry_not_the_address_shape():
    """Guessing a family from the string is how a typo becomes a different
    chain. The caller names the chain; the registry says what it is."""
    assert family_of("base") == "evm"
    assert family_of("solana") == "svm"
    assert family_of("nonesuch") is None


# -- evm ---------------------------------------------------------------------

def test_an_evm_address_checksums_on_an_evm_chain():
    assert normalize_for_chain("base", EVM.lower()) == EVM


def test_a_failed_eip55_checksum_is_still_refused():
    bad = EVM[:10] + ("A" if EVM[10] != "A" else "b") + EVM[11:]
    with pytest.raises(ValueError, match="checksum"):
        normalize_for_chain("base", bad)


def test_a_solana_address_is_refused_on_an_evm_chain():
    with pytest.raises(ValueError):
        normalize_for_chain("base", SOL_USDC)


# -- svm ---------------------------------------------------------------------

def test_a_solana_mint_is_accepted_verbatim():
    """Base58 is CASE-SENSITIVE — normalizing case would destroy the address.
    (invoicing.py's recipient.lower() is the live example of that bug class.)"""
    assert normalize_for_chain("solana", SOL_USDC) == SOL_USDC
    assert normalize_for_chain("solana", SOL_WSOL) == SOL_WSOL


def test_a_solana_address_is_never_case_folded():
    with pytest.raises(ValueError):
        normalize_for_chain("solana", SOL_USDC.lower())


def test_an_evm_address_is_refused_on_solana():
    with pytest.raises(ValueError):
        normalize_for_chain("solana", EVM)


def test_a_base58_string_of_the_wrong_length_is_refused():
    with pytest.raises(ValueError):
        normalize_for_chain("solana", "abc")


def test_a_string_with_base58_excluded_characters_is_refused():
    """0, O, I and l are excluded from the alphabet precisely because they are
    the characters people confuse."""
    for ch in ("0", "O", "I", "l"):
        with pytest.raises(ValueError):
            normalize_for_chain("solana", SOL_USDC[:-1] + ch)


def test_an_unknown_chain_refuses_rather_than_defaulting_to_evm():
    """chains.py's rule: an unknown chain resolves to nothing, and the caller
    must refuse. A default family would validate a Solana mint as Ethereum."""
    with pytest.raises(ValueError, match="unknown"):
        normalize_for_chain("nonesuch", EVM)


# -- the honesty the caller needs --------------------------------------------

def test_the_typo_note_says_evm_HAS_a_checksum():
    assert "checksum" in typo_protection_note("base").lower()


def test_the_typo_note_says_solana_has_NONE():
    note = typo_protection_note("solana").lower()
    assert "no checksum" in note or "not have a checksum" in note
    assert "verify" in note or "confirm" in note
