"""SPL instruction encoding (042b).

Two independent checks on every layout: the exact bytes, written down here so a
silent edit fails; and a cross-check against ``spl.token.instructions`` as an
ORACLE. The oracle is import-skipped and must never become a runtime
dependency — it exists in this environment as an x402 transitive, and its own
instruction enum stops before `InitializeMint2`, so it cannot build the
instruction that matters most.
"""
import struct

import pytest

solders = pytest.importorskip("solders")

from core.wallet import spl_token as S  # noqa: E402

from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

A, B, C = Keypair().pubkey(), Keypair().pubkey(), Keypair().pubkey()


# ==========================================================================
# Exact bytes
# ==========================================================================

def test_create_account_discriminator_is_a_u32_not_a_u8():
    """Unlike every SPL Token instruction, System's is a u32. Getting it wrong
    shifts every subsequent field by three bytes."""
    ix = S.ix_create_account(payer=str(A), new_account=str(B),
                             lamports=1_461_600, space=82,
                             owner=S.TOKEN_PROGRAM)
    data = bytes(ix.data)
    assert len(data) == 52
    assert data[0:4] == struct.pack("<I", 0)
    assert data[4:12] == struct.pack("<Q", 1_461_600)
    assert data[12:20] == struct.pack("<Q", 82)
    assert data[20:52] == bytes(Pubkey.from_string(S.TOKEN_PROGRAM))
    assert [(m.is_signer, m.is_writable) for m in ix.accounts] == [(True, True), (True, True)]


def test_initialize_mint2_is_index_20_and_67_bytes():
    ix = S.ix_initialize_mint2(mint=str(A), decimals=9, mint_authority=str(B))
    data = bytes(ix.data)
    assert len(data) == 67
    assert data[0] == 20            # InitializeMint2, NOT InitializeMint (0)
    assert data[1] == 9
    assert data[2:34] == bytes(B)
    assert len(ix.accounts) == 1    # the whole point: no rent Sysvar
    assert (ix.accounts[0].is_signer, ix.accounts[0].is_writable) == (False, True)


def test_a_coption_pubkey_is_33_bytes_either_way():
    """SPL's COption is FIXED-width. Borsh's variable-length Option is a
    different encoding, and using it produces a length the program reports as
    an opaque InvalidInstructionData."""
    absent = bytes(S.ix_initialize_mint2(mint=str(A), decimals=9,
                                         mint_authority=str(B)).data)
    present = bytes(S.ix_initialize_mint2(mint=str(A), decimals=9,
                                          mint_authority=str(B),
                                          freeze_authority=str(C)).data)
    assert len(absent) == len(present) == 67
    assert absent[34] == 0x00 and absent[35:] == bytes(32)
    assert present[34] == 0x01 and present[35:] == bytes(C)


def test_mint_to_is_index_7_and_a_u64():
    ix = S.ix_mint_to(mint=str(A), destination=str(B), authority=str(C),
                      amount=10 ** 18)
    data = bytes(ix.data)
    assert data == bytes([7]) + struct.pack("<Q", 10 ** 18)
    assert [(m.is_signer, m.is_writable) for m in ix.accounts] == [
        (False, True), (False, True), (True, False)]


def test_set_authority_revokes_with_a_zero_option():
    ix = S.ix_set_authority(account=str(A), current_authority=str(B),
                            authority_type=S.AUTHORITY_MINT_TOKENS,
                            new_authority=None)
    data = bytes(ix.data)
    assert len(data) == 35
    assert data[0] == 6 and data[1] == S.AUTHORITY_MINT_TOKENS
    assert data[2] == 0x00 and data[3:] == bytes(32)


def test_the_ata_create_is_idempotent_and_carries_six_accounts():
    """The legacy empty-data form takes SEVEN accounts (it carries the rent
    Sysvar). Mixing the data byte with the wrong account count is the trap."""
    ix, ata = S.ix_create_ata_idempotent(payer=str(A), owner=str(B), mint=str(C))
    assert str(ix.accounts[5].pubkey) == S.TOKEN_2022_PROGRAM
    assert bytes(ix.data) == b"\x01"
    assert len(ix.accounts) == 6
    assert str(ix.accounts[1].pubkey) == ata


@pytest.mark.parametrize("program", ["TOKEN_PROGRAM", "TOKEN_2022_PROGRAM"])
def test_the_ata_is_the_canonical_pda_under_either_program(program):
    """⚠️ The token program is a SEED. A mint created under Token-2022 has a
    different ATA than the same mint under classic, so deriving with the wrong
    one addresses an account that will never exist."""
    prog = getattr(S, program)
    _ix, ata = S.ix_create_ata_idempotent(payer=str(A), owner=str(B), mint=str(C),
                                          token_program=prog)
    expected, _bump = Pubkey.find_program_address(
        [bytes(B), bytes(Pubkey.from_string(prog)), bytes(C)],
        Pubkey.from_string(S.ATA_PROGRAM))
    assert ata == str(expected)


# ==========================================================================
# The independent oracle
# ==========================================================================

@pytest.fixture()
def oracle():
    return pytest.importorskip(
        "spl.token.instructions",
        reason="solana-py is an extras-only oracle, never a runtime dependency")


def test_mint_to_matches_the_oracle(oracle):
    """The oracle only speaks classic SPL, so the comparison pins the ENCODING
    (which is identical across both programs) rather than the program id."""
    from spl.token.constants import TOKEN_PROGRAM_ID
    mine = S.ix_mint_to(mint=str(A), destination=str(B), authority=str(C),
                        amount=123_456_789, token_program=S.TOKEN_PROGRAM)
    theirs = oracle.mint_to(oracle.MintToParams(
        program_id=TOKEN_PROGRAM_ID, mint=A, dest=B, mint_authority=C,
        amount=123_456_789))
    assert bytes(mine.data) == bytes(theirs.data)
    assert _shape(mine) == _shape(theirs)


def test_set_authority_matches_the_oracle(oracle):
    from spl.token.constants import TOKEN_PROGRAM_ID
    mine = S.ix_set_authority(account=str(A), current_authority=str(B),
                              authority_type=S.AUTHORITY_MINT_TOKENS,
                              new_authority=None,
                              token_program=S.TOKEN_PROGRAM)
    theirs = oracle.set_authority(oracle.SetAuthorityParams(
        program_id=TOKEN_PROGRAM_ID, account=A,
        authority=oracle.AuthorityType.MINT_TOKENS, current_authority=B,
        new_authority=None))
    assert bytes(mine.data) == bytes(theirs.data)
    assert _shape(mine) == _shape(theirs)


def test_the_ata_instruction_matches_the_oracle(oracle):
    mine, ata = S.ix_create_ata_idempotent(payer=str(A), owner=str(B), mint=str(C),
                                           token_program=S.TOKEN_PROGRAM)
    theirs = oracle.create_idempotent_associated_token_account(
        payer=A, owner=B, mint=C)
    assert bytes(mine.data) == bytes(theirs.data)
    assert _shape(mine) == _shape(theirs)
    assert ata == str(oracle.get_associated_token_address(B, C))


def test_the_account_sizes_match_the_oracle(oracle):
    from spl.token.constants import ACCOUNT_LEN, MINT_LEN, TOKEN_PROGRAM_ID
    assert S.MINT_LEN == MINT_LEN
    assert S.TOKEN_ACCOUNT_LEN == ACCOUNT_LEN
    assert S.TOKEN_PROGRAM == str(TOKEN_PROGRAM_ID)


def _shape(ix):
    return [(str(m.pubkey), m.is_signer, m.is_writable) for m in ix.accounts]


# ==========================================================================
# Rent
# ==========================================================================

def test_the_formula_reproduces_the_MEASURED_mainnet_answers():
    """⚠️ Measured live 2026-09-13, not recalled. The widely-quoted 3480
    lamports/byte-year gives 1,461,600 for an 82-byte mint; mainnet answers
    1,066,800. The overhead (128) and threshold (2.0) are unchanged; the
    per-byte-year figure is 2540."""
    assert S.rent_exempt_lamports(S.MINT_LEN) == 1_066_800
    assert S.rent_exempt_lamports(S.TOKEN_ACCOUNT_LEN) == 1_488_440
    assert S.rent_exempt_lamports(0) == 650_240


def test_the_rpc_answer_wins_when_it_has_one():
    """It is authoritative — the constants above were 37% wrong once already."""
    assert S.rent_exempt_from_rpc(S.MINT_LEN, lambda m, p: 999_999) == 999_999


def test_an_unreachable_rpc_falls_back_WITH_A_MARGIN():
    """Fail-OPEN, but generously: the failure directions are not symmetric.
    Over-funding leaves recoverable lamports in an account we own; under-funding
    fails two instructions later with an InvalidAccountData far from its cause.
    """
    def _dead(_method, _params):
        raise RuntimeError("rpc down")
    got = S.rent_exempt_from_rpc(S.MINT_LEN, _dead)
    assert got > S.rent_exempt_lamports(S.MINT_LEN)
    assert got == int(1_066_800 * 1.25)


@pytest.mark.parametrize("bad", [0, -1, "nonsense", None])
def test_a_nonsense_rpc_answer_falls_back(bad):
    assert S.rent_exempt_from_rpc(S.MINT_LEN, lambda m, p: bad) == int(1_066_800 * 1.25)


# ==========================================================================
# The transaction
# ==========================================================================

def _build(**kw):
    payer = kw.pop("payer", None) or Keypair()
    base = dict(payer=str(payer.pubkey() if hasattr(payer, "pubkey") else payer),
                decimals=9, supply_raw=10 ** 18,
                recent_blockhash=str(Hash.default()),
                name="Rob Coin", symbol="ROB", uri="https://x/y.json",
                mint_rent=S.rent_exempt_lamports(S.MINT_WITH_POINTER_LEN))
    base.update(kw)
    return S.build_fixed_supply_mint(**base), payer


def test_the_transaction_closes_everything_it_opens():
    """Eight instructions, and the ORDER is the safety argument: the metadata
    pointer must precede initialization (Token-2022 refuses an extension on an
    initialized mint), and both authorities are revoked in the SAME transaction
    that creates what they control."""
    (tx, _kp, mint, _ata), _payer = _build()
    programs = [str(tx.message.account_keys[i.program_id_index])
                for i in tx.message.instructions]
    assert programs == [S.SYSTEM_PROGRAM, S.TOKEN_2022_PROGRAM,
                        S.TOKEN_2022_PROGRAM, S.TOKEN_2022_PROGRAM,
                        S.TOKEN_2022_PROGRAM, S.ATA_PROGRAM,
                        S.TOKEN_2022_PROGRAM, S.TOKEN_2022_PROGRAM]

    ptr = bytes(tx.message.instructions[1].data)
    assert ptr[0] == 39 and ptr[1] == 0
    assert ptr[2:34] == bytes(32)                      # no pointer authority
    assert ptr[34:66] == bytes(_kp_pubkey(mint))       # points at ITSELF

    init = bytes(tx.message.instructions[2].data)
    assert init[0] == 20 and init[34] == 0x00          # NO freeze authority

    meta = bytes(tx.message.instructions[3].data)
    assert meta[:8] == S._spl_discriminate(
        "spl_token_metadata_interface:initialize_account")

    revoke_meta = bytes(tx.message.instructions[4].data)
    assert revoke_meta[:8] == S._spl_discriminate(
        "spl_token_metadata_interface:update_the_authority")
    assert revoke_meta[8:] == bytes(32)                # -> nobody

    revoke_mint = bytes(tx.message.instructions[7].data)
    assert revoke_mint[0] == 6 and revoke_mint[1] == S.AUTHORITY_MINT_TOKENS
    assert revoke_mint[2] == 0x00


def _kp_pubkey(addr):
    return bytes(Pubkey.from_string(addr))


def test_a_token_with_no_name_or_symbol_is_refused():
    """They are written on-chain and are what every explorer shows. A blank one
    is the 'Unknown token' this path exists to avoid."""
    with pytest.raises(S.SplBuildError) as exc:
        _build(name="", symbol="ROB")
    assert "name and a symbol" in str(exc.value)


def test_the_metadata_discriminators_are_DERIVED_not_pasted():
    """sha256 of the interface namespace, first 8 bytes. Pinned against the
    values a live mainnet simulation accepted."""
    assert S._spl_discriminate(
        "spl_token_metadata_interface:initialize_account").hex() == "d2e11ea258b84d8d"
    assert S._spl_discriminate(
        "spl_token_metadata_interface:update_the_authority").hex() == "d7e4a6e45464567b"


def test_the_mint_is_created_under_TOKEN_2022_not_classic():
    """Classic SPL has no metadata at all. That is the whole reason for the
    program choice."""
    (tx, _kp, _mint, _ata), _payer = _build()
    create = bytes(tx.message.instructions[0].data)
    assert create[20:52] == bytes(Pubkey.from_string(S.TOKEN_2022_PROGRAM))


def test_it_requires_exactly_two_signatures_with_the_payer_first():
    (tx, kp, mint, _ata), payer = _build()
    signers = S.signer_pubkeys(tx)
    assert signers == [str(payer.pubkey()), mint]
    assert str(kp.pubkey()) == mint


def test_it_fits_in_one_transaction_without_lookup_tables():
    """~722 bytes against a 1232 limit, so the ALT question never arises — and
    `relay_svm` records why decoding one is refused rather than attempted."""
    (tx, _kp, _mint, _ata), _payer = _build()
    assert len(bytes(tx)) < 900
    assert len(bytes(tx)) <= S.MAX_TX_BYTES


def test_the_existing_program_allowlist_accepts_it_unwidened():
    """No `extra_allowed` is needed: System, SPL Token and the ATA program are
    all already in `BASE_ALLOWED_PROGRAMS`."""
    from core.wallet import solana_tx_inspect as inspect

    (tx, _kp, _mint, _ata), _payer = _build()
    result = inspect.inspect_transaction(bytes(tx))
    assert result.ok, result.reason
    assert set(result.program_ids) == {S.SYSTEM_PROGRAM, S.TOKEN_2022_PROGRAM,
                                       S.ATA_PROGRAM}


@pytest.mark.parametrize("decimals", [-1, 10, 255])
def test_out_of_range_decimals_refuse(decimals):
    with pytest.raises(S.SplBuildError):
        S.ix_initialize_mint2(mint=str(A), decimals=decimals, mint_authority=str(B))


def test_a_supply_that_does_not_fit_a_u64_refuses():
    with pytest.raises(S.SplBuildError):
        S.ix_mint_to(mint=str(A), destination=str(B), authority=str(C),
                     amount=2 ** 64)


def test_a_zero_mint_refuses():
    with pytest.raises(S.SplBuildError):
        S.ix_mint_to(mint=str(A), destination=str(B), authority=str(C), amount=0)


# ==========================================================================
# Reading the mint back
# ==========================================================================

def _mint_info(**over):
    info = {"decimals": 9, "supply": "1000000000000000000",
            "mintAuthority": None, "freezeAuthority": None,
            "isInitialized": True}
    info.update(over)
    return {"value": {"data": {"program": "spl-token", "parsed": {"info": info}}}}


def test_a_revoked_mint_decodes_as_revoked():
    state = S.decode_mint_account(_mint_info())
    assert state["mint_authority"] is None and state["freeze_authority"] is None
    assert state["supply"] == 10 ** 18


def test_a_live_mint_authority_is_visible():
    state = S.decode_mint_account(_mint_info(mintAuthority=str(A)))
    assert state["mint_authority"] == str(A)


@pytest.mark.parametrize("payload", [
    None, {}, {"value": None}, {"value": {"data": "base64blob"}},
    {"value": {"data": {"program": "stake", "parsed": {"info": {}}}}},
])
def test_anything_that_is_not_a_mint_decodes_to_None(payload):
    """None means UNKNOWN and the caller renders UNVERIFIED — it must never
    collapse into 'no authority', which is the reassuring reading."""
    assert S.decode_mint_account(payload) is None
