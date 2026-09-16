"""Building Relay's Solana order into a signable transaction (037).

The address-lookup tables are deliberately unused: they only COMPRESS a message,
and mis-decoding one resolves an account index to the WRONG account inside a
transaction we then sign. These tests pin that choice and its escape hatch.
"""
import pytest
from solders.hash import Hash
from solders.keypair import Keypair

from core.wallet.relay_svm import (MAX_TX_BYTES, RelaySvmBuildError,
                                   build_transaction, signer_accounts)

PAYER = str(Keypair().pubkey())
OTHER = str(Keypair().pubkey())
PROGRAM = str(Keypair().pubkey())


def _ix(data="0d9e0ddf", signer=PAYER, extra=()):
    keys = [{"pubkey": signer, "isSigner": True, "isWritable": True}]
    keys += [{"pubkey": k, "isSigner": False, "isWritable": True} for k in extra]
    return {"programId": PROGRAM, "keys": keys, "data": data}


def test_it_builds_a_transaction_from_relay_instructions():
    tx = build_transaction(instructions=[_ix()], payer=PAYER,
                           recent_blockhash=str(Hash.default()))
    assert 0 < len(bytes(tx)) <= MAX_TX_BYTES


def test_hex_and_base64_instruction_data_both_decode():
    a = build_transaction(instructions=[_ix("0d9e")], payer=PAYER,
                          recent_blockhash=str(Hash.default()))
    b = build_transaction(instructions=[_ix("DZ4=")], payer=PAYER,
                          recent_blockhash=str(Hash.default()))
    assert bytes(a) and bytes(b)


def test_undecodable_data_raises_rather_than_becoming_empty_bytes():
    """Empty bytes would call the program with no arguments — a different order."""
    with pytest.raises(RelaySvmBuildError, match="neither hex nor base64"):
        build_transaction(instructions=[_ix("zzz!!!not-data")], payer=PAYER,
                          recent_blockhash=str(Hash.default()))


def test_an_instruction_with_no_data_raises():
    with pytest.raises(RelaySvmBuildError, match="no data"):
        build_transaction(instructions=[_ix("")], payer=PAYER,
                          recent_blockhash=str(Hash.default()))


def test_an_instruction_with_no_program_raises():
    ix = _ix()
    ix["programId"] = ""
    with pytest.raises(RelaySvmBuildError, match="no programId"):
        build_transaction(instructions=[ix], payer=PAYER,
                          recent_blockhash=str(Hash.default()))


def test_an_instruction_naming_no_accounts_raises():
    with pytest.raises(RelaySvmBuildError, match="names no accounts"):
        build_transaction(instructions=[{"programId": PROGRAM, "keys": [], "data": "00"}],
                          payer=PAYER, recent_blockhash=str(Hash.default()))


def test_an_oversized_transaction_is_refused_not_compressed():
    """The escape hatch: refuse the route rather than start decoding tables."""
    fat = [_ix(extra=[str(Keypair().pubkey()) for _ in range(40)]) for _ in range(3)]
    with pytest.raises(RelaySvmBuildError, match="over Solana's"):
        build_transaction(instructions=fat, payer=PAYER,
                          recent_blockhash=str(Hash.default()))


def test_signer_accounts_reports_exactly_the_required_signers():
    assert signer_accounts([_ix()]) == {PAYER}
    assert signer_accounts([_ix(), _ix(signer=OTHER)]) == {PAYER, OTHER}


def test_signer_accounts_is_empty_for_no_instructions():
    assert signer_accounts([]) == set()


# --- the bridge-scoped program widening (037) -----------------------------

def test_the_relay_program_is_NOT_in_the_default_allowlist():
    """A SWAP that suddenly calls Relay's deposit program is exactly the anomaly
    the allowlist exists to catch. Only the bridge verb may pass it in."""
    from core.wallet.solana_tx_inspect import (BASE_ALLOWED_PROGRAMS,
                                               RELAY_PROGRAM_IDS,
                                               allowed_programs)
    assert RELAY_PROGRAM_IDS
    assert not (RELAY_PROGRAM_IDS & BASE_ALLOWED_PROGRAMS)
    assert not (RELAY_PROGRAM_IDS & allowed_programs())


def test_an_unknown_program_still_refuses_without_the_widening():
    from solders.hash import Hash

    from core.wallet.relay_svm import build_transaction
    from core.wallet.solana_tx_inspect import inspect_transaction
    kp = Keypair()
    ix = {"programId": str(Keypair().pubkey()),
          "keys": [{"pubkey": str(kp.pubkey()), "isSigner": True, "isWritable": True}],
          "data": "0d9e"}
    tx = build_transaction(instructions=[ix], payer=str(kp.pubkey()),
                           recent_blockhash=str(Hash.default()))
    assert inspect_transaction(bytes(tx)).ok is False


def test_the_widening_is_per_call_and_additive():
    """Passing the pinned set lets exactly that program through, and nothing else."""
    from solders.hash import Hash

    from core.wallet.relay_svm import build_transaction
    from core.wallet.solana_tx_inspect import (RELAY_PROGRAM_IDS,
                                               inspect_transaction)
    kp = Keypair()
    relay = sorted(RELAY_PROGRAM_IDS)[0]
    ix = {"programId": relay,
          "keys": [{"pubkey": str(kp.pubkey()), "isSigner": True, "isWritable": True}],
          "data": "0d9e"}
    tx = build_transaction(instructions=[ix], payer=str(kp.pubkey()),
                           recent_blockhash=str(Hash.default()))
    raw = bytes(tx)
    assert inspect_transaction(raw).ok is False, "must refuse without the widening"
    assert inspect_transaction(raw, extra_allowed=RELAY_PROGRAM_IDS).ok is True


def test_the_signature_array_matches_the_required_signer_count():
    """A transaction is only well-formed when len(signatures) ==
    header.num_required_signatures. Built with [], the RPC rejects it as
    'Transaction failed to sanitize accounts offsets correctly' — an error that
    names offsets and has nothing to do with them. Found on prod."""
    tx = build_transaction(instructions=[_ix()], payer=PAYER,
                           recent_blockhash=str(Hash.default()))
    assert len(tx.signatures) == tx.message.header.num_required_signatures == 1


def test_the_placeholders_are_zeroed_and_cannot_validate():
    """They shape the bytes for simulateTransaction (which runs sigVerify off);
    they are not signatures and the signer replaces them wholesale."""
    from solders.signature import Signature
    tx = build_transaction(instructions=[_ix()], payer=PAYER,
                           recent_blockhash=str(Hash.default()))
    assert all(str(s) == str(Signature.default()) for s in tx.signatures)


def test_signing_replaces_the_placeholder_with_a_real_signature():
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    kp = Keypair()
    ix = {"programId": PROGRAM,
          "keys": [{"pubkey": str(kp.pubkey()), "isSigner": True, "isWritable": True}],
          "data": "0d9e"}
    tx = build_transaction(instructions=[ix], payer=str(kp.pubkey()),
                           recent_blockhash=str(Hash.default()))
    signed = VersionedTransaction(tx.message, [kp])
    assert str(signed.signatures[0]) != str(Signature.default())


# --- the simulation account cap (037, measured on prod) -------------------

def test_the_sim_account_cap_defaults_to_the_measured_provider_limit(monkeypatch):
    """Alchemy refuses more than 5 and FAILS the whole simulation rather than
    truncating, so the old default of 100 made every Solana money verb unusable
    on the pinned RPC."""
    from core.wallet.solana_tx_inspect import sim_max_addresses
    monkeypatch.delenv("DEFI_SOLANA_SIM_MAX_ACCOUNTS", raising=False)
    assert sim_max_addresses() == 5


def test_the_cap_is_operator_tunable_and_never_below_one(monkeypatch):
    from core.wallet.solana_tx_inspect import sim_max_addresses
    monkeypatch.setenv("DEFI_SOLANA_SIM_MAX_ACCOUNTS", "64")
    assert sim_max_addresses() == 64
    monkeypatch.setenv("DEFI_SOLANA_SIM_MAX_ACCOUNTS", "0")
    assert sim_max_addresses() == 1


def test_the_owner_survives_truncation():
    """Ordering is the safety property: the owner is first, so the assertion
    that bounds the outflow is the one that survives a cut."""
    from core.wallet.solana_tx_inspect import simulation_addresses
    keys = [str(Keypair().pubkey()) for _ in range(30)]
    got = simulation_addresses(PAYER, mints=(), rpc=None, account_keys=keys)
    assert got[0] == PAYER
    assert len(got) <= 5
