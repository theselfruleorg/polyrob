"""The send builders in ``core.wallet.spl_token`` (native SOL + SPL TransferChecked).

Same two-check discipline as ``test_spl_token.py``: the exact bytes, and an
independent oracle (solders' own System builder; ``spl.token.instructions`` for
TransferChecked, import-skipped).
"""
import struct

import pytest

solders = pytest.importorskip("solders")

from core.wallet import spl_token as S  # noqa: E402

from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

A, B, M = Keypair().pubkey(), Keypair().pubkey(), Keypair().pubkey()
BH = str(Hash.default())


def test_system_transfer_bytes_match_solders():
    from solders.system_program import TransferParams, transfer
    ix = S.ix_system_transfer(source=str(A), destination=str(B), lamports=123_456)
    oracle = transfer(TransferParams(from_pubkey=A, to_pubkey=B, lamports=123_456))
    assert bytes(ix.data) == bytes(oracle.data)
    assert bytes(ix.data) == struct.pack("<I", 2) + struct.pack("<Q", 123_456)
    assert list(ix.accounts) == list(oracle.accounts)


@pytest.mark.parametrize("bad", [0, -1, 2 ** 64])
def test_system_transfer_refuses_out_of_range(bad):
    with pytest.raises(S.SplBuildError):
        S.ix_system_transfer(source=str(A), destination=str(B), lamports=bad)


def test_transfer_checked_is_index_12_and_10_bytes():
    ix = S.ix_transfer_checked(source=str(A), mint=str(M), destination=str(B),
                               owner=str(A), amount=5_000_000, decimals=6)
    data = bytes(ix.data)
    assert data == bytes([12]) + struct.pack("<Q", 5_000_000) + bytes([6])
    assert [(str(m.pubkey), m.is_signer, m.is_writable) for m in ix.accounts] == [
        (str(A), False, True), (str(M), False, False), (str(B), False, True),
        (str(A), True, False)]
    assert str(ix.program_id) == S.TOKEN_PROGRAM


def test_transfer_checked_matches_the_spl_oracle():
    spl_ix = pytest.importorskip("spl.token.instructions")
    ours = S.ix_transfer_checked(source=str(A), mint=str(M), destination=str(B),
                                 owner=str(A), amount=42, decimals=9)
    oracle = spl_ix.transfer_checked(spl_ix.TransferCheckedParams(
        program_id=Pubkey.from_string(S.TOKEN_PROGRAM), source=A, mint=M, dest=B,
        owner=A, amount=42, decimals=9))
    assert bytes(ours.data) == bytes(oracle.data)
    assert [(m.pubkey, m.is_signer, m.is_writable) for m in ours.accounts] == \
        [(m.pubkey, m.is_signer, m.is_writable) for m in oracle.accounts]


def test_build_send_native_is_one_system_transfer_one_signer():
    tx, dest = S.build_send(payer=str(A), to=str(B), amount_raw=1_000_000,
                            recent_blockhash=BH)
    assert dest == str(B)
    msg = tx.message
    assert msg.header.num_required_signatures == 1
    assert str(msg.account_keys[0]) == str(A)
    assert len(msg.instructions) == 1
    ix = msg.instructions[0]
    assert str(msg.account_keys[ix.program_id_index]) == S.SYSTEM_PROGRAM
    assert bytes(ix.data) == struct.pack("<I", 2) + struct.pack("<Q", 1_000_000)


@pytest.mark.parametrize("program", [S.TOKEN_PROGRAM, S.TOKEN_2022_PROGRAM])
def test_build_send_spl_creates_recipient_ata_then_transfer_checked(program):
    tx, dest = S.build_send(payer=str(A), to=str(B), amount_raw=7, decimals=6,
                            mint=str(M), token_program=program,
                            recent_blockhash=BH)
    assert dest == S.associated_token_address(str(B), str(M), program)
    msg = tx.message
    keys = [str(k) for k in msg.account_keys]
    assert msg.header.num_required_signatures == 1 and keys[0] == str(A)
    create, xfer = msg.instructions
    assert keys[create.program_id_index] == S.ATA_PROGRAM
    assert bytes(create.data) == b"\x01"                 # CreateIdempotent
    assert keys[bytes(create.accounts)[1]] == dest
    assert keys[bytes(create.accounts)[2]] == str(B)     # the recipient owns it
    assert keys[xfer.program_id_index] == program
    assert bytes(xfer.data)[0] == 12
    src = S.associated_token_address(str(A), str(M), program)
    assert [keys[i] for i in bytes(xfer.accounts)] == [src, str(M), dest, str(A)]


def test_build_send_spl_refuses_without_decimals_or_program():
    with pytest.raises(S.SplBuildError):
        S.build_send(payer=str(A), to=str(B), amount_raw=7, mint=str(M),
                     token_program=S.TOKEN_PROGRAM, recent_blockhash=BH)
    with pytest.raises(S.SplBuildError):
        S.build_send(payer=str(A), to=str(B), amount_raw=7, mint=str(M),
                     decimals=6, token_program="x", recent_blockhash=BH)


def test_decode_mint_account_reports_type_program_and_extensions():
    info = {"value": {"owner": S.TOKEN_2022_PROGRAM, "data": {
        "program": "spl-token-2022", "parsed": {"type": "mint", "info": {
            "decimals": 6, "supply": "10", "mintAuthority": None,
            "freezeAuthority": None, "isInitialized": True,
            "extensions": [{"extension": "transferFeeConfig",
                            "state": {"newerTransferFee": {"transferFeeBasisPoints": 50}}}],
        }}}}}
    state = S.decode_mint_account(info)
    assert state["type"] == "mint"
    assert state["program"] == S.TOKEN_2022_PROGRAM
    assert state["extensions"][0][0] == "transferFeeConfig"
    classic = {"value": {"data": {"program": "spl-token", "parsed": {
        "type": "account", "info": {"mint": str(M)}}}}}
    state = S.decode_mint_account(classic)
    assert state["type"] == "account" and state["program"] == S.TOKEN_PROGRAM
