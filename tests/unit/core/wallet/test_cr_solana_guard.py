"""CR-H04 / CR-H05 / CR-M05 / CR-L06 / CR-L07 — the Solana guard decodes the
instructions and observes every account it must (crypto security analysis
2026-09-23)."""
import struct

import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.wallet import solana_simulation as ss
from core.wallet import solana_tx_inspect as sti

CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
SYSTEM = sti.SYSTEM_PROGRAM_ID
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ATTACKER = "7WktogJEd2wQ9eH2oWusmcoFTgeYi6rS632UviTBJ2jm"


def _tx(ixs, kp=None, extra_signers=()):
    """``ixs`` = [(program, data, [(pubkey, signer, writable), ...]), ...]."""
    kp = kp or Keypair()
    built = [Instruction(Pubkey.from_string(p), data,
                         [AccountMeta(Pubkey.from_string(str(k)), s, w)
                          for k, s, w in metas])
             for p, data, metas in ixs]
    msg = MessageV0.try_compile(kp.pubkey(), built, [], Hash.default())
    return bytes(VersionedTransaction(msg, [kp, *extra_signers])), str(kp.pubkey())


def _sys(tag, extra=b""):
    return struct.pack("<I", tag) + extra


# -- CR-H05: token instructions are decoded, not only their program ---------

@pytest.mark.parametrize("tag,name", [(4, "Approve"), (13, "ApproveChecked"),
                                      (6, "SetAuthority"), (10, "FreezeAccount")])
def test_a_grant_on_an_existing_account_refuses(tag, name):
    kp = Keypair()
    owner = str(kp.pubkey())
    victim = str(Pubkey.new_unique())
    raw, _ = _tx([(JUP, b"\x00", [(owner, True, True)]),
                  (CLASSIC, bytes([tag]) + bytes(9),
                   [(victim, False, True), (ATTACKER, False, False),
                    (owner, True, False)])], kp)
    got = sti.inspect_transaction(raw)
    assert got.ok is False
    assert name in got.reason


def test_a_close_to_a_foreign_destination_refuses():
    kp = Keypair()
    owner = str(kp.pubkey())
    acct = str(Pubkey.new_unique())
    raw, _ = _tx([(CLASSIC, bytes([9]),
                   [(acct, False, True), (ATTACKER, False, True),
                    (owner, True, False)])], kp)
    got = sti.inspect_transaction(raw)
    assert got.ok is False and "closes a token account" in got.reason


def test_a_close_back_to_the_owner_is_the_ordinary_wsol_unwrap():
    kp = Keypair()
    owner = str(kp.pubkey())
    acct = str(Pubkey.new_unique())
    raw, _ = _tx([(CLASSIC, bytes([9]),
                   [(acct, False, True), (owner, True, True)])], kp)
    assert sti.inspect_transaction(raw).ok is True


def test_set_authority_on_an_account_this_tx_creates_is_allowed():
    """The SPL deploy revokes the mint authority of the mint it just created."""
    kp, mint_kp = Keypair(), Keypair()
    owner, mint = str(kp.pubkey()), str(mint_kp.pubkey())
    raw, _ = _tx([(SYSTEM, _sys(0, struct.pack("<QQ", 1, 82)) + bytes(32),
                   [(owner, True, True), (mint, True, True)]),
                  (CLASSIC, bytes([6, 0, 0]),
                   [(mint, False, True), (owner, True, False)])],
                 kp, extra_signers=[mint_kp])
    assert sti.inspect_transaction(raw).ok is True


def test_idempotent_create_does_not_prove_the_account_is_new():
    """CreateIdempotent is a no-op on an existing account, so a grant right
    after it may be on a position we already hold."""
    kp = Keypair()
    owner = str(kp.pubkey())
    ata = str(Pubkey.new_unique())
    raw, _ = _tx([(sti.ASSOCIATED_TOKEN_PROGRAM_ID, b"\x01",
                   [(owner, True, True), (ata, False, True), (owner, True, False),
                    (MINT, False, False)]),
                  (CLASSIC, bytes([4]) + bytes(8),
                   [(ata, False, True), (ATTACKER, False, False),
                    (owner, True, False)])], kp)
    assert sti.inspect_transaction(raw).ok is False


def test_assign_of_the_wallet_account_refuses():
    kp = Keypair()
    owner = str(kp.pubkey())
    raw, _ = _tx([(SYSTEM, _sys(1) + bytes(32), [(owner, True, True)])], kp)
    got = sti.inspect_transaction(raw)
    assert got.ok is False and "re-assigns" in got.reason


# -- CR-M05: the durable-nonce family ----------------------------------------

def test_a_durable_nonce_transaction_refuses():
    kp = Keypair()
    owner = str(kp.pubkey())
    nonce = str(Pubkey.new_unique())
    raw, _ = _tx([(SYSTEM, _sys(4),
                   [(nonce, False, True),
                    ("SysvarRecentB1ockHashes11111111111111111111", False, False),
                    (owner, True, False)]),
                  (JUP, b"\x00", [(owner, True, True)])], kp)
    got = sti.inspect_transaction(raw)
    assert got.ok is False and "durable-nonce" in got.reason


def test_an_excessive_priority_fee_refuses():
    kp = Keypair()
    owner = str(kp.pubkey())
    cb = sti.COMPUTE_BUDGET_PROGRAM_ID
    raw, _ = _tx([(cb, bytes([2]) + struct.pack("<I", 1_400_000), []),
                  (cb, bytes([3]) + struct.pack("<Q", 10_000_000_000), []),
                  (JUP, b"\x00", [(owner, True, True)])], kp)
    got = sti.inspect_transaction(raw)
    assert got.ok is False and "fee" in got.reason


def test_the_fee_is_base_plus_priority():
    cb = sti.COMPUTE_BUDGET_PROGRAM_ID
    ixs = [sti.DecodedIx(cb, bytes([2]) + struct.pack("<I", 100_000), ()),
           sti.DecodedIx(cb, bytes([3]) + struct.pack("<Q", 2_000_000), ()),
           sti.DecodedIx(JUP, b"", ())]
    assert sti.fee_lamports(ixs, 1) == 5_000 + 200_000


# -- CR-H05: the observation is split, never truncated -----------------------

def _acct(owner, mint=MINT, amount="1000", lamports=2_039_280, **extra):
    info = {"owner": owner, "mint": mint,
            "tokenAmount": {"amount": amount, "decimals": 6},
            "state": "initialized", **extra}
    return {"lamports": lamports, "owner": CLASSIC,
            "data": {"parsed": {"type": "account", "info": info}}}


def _plain(lamports):
    return {"lamports": lamports, "owner": SYSTEM}


def test_every_owned_account_is_observed_across_several_simulations(monkeypatch):
    """Nine owned accounts under a 5-account RPC cap: the drain on the LAST
    one used to be invisible (truncated); now it is seen."""
    monkeypatch.delenv("DEFI_SOLANA_SIM_MAX_ACCOUNTS", raising=False)
    kp = Keypair()
    mine = [str(Pubkey.new_unique()) for _ in range(9)]
    # The route names every one of them (an account a transaction does not name cannot change).
    raw, owner = _tx([(JUP, b"\x00", [(str(kp.pubkey()), True, True)]
                       + [(a, False, True) for a in mine])], kp)
    last = mine[-1]
    sims = []

    def _rpc(method, params):
        if method == "getTokenAccountsByOwner":
            return {"value": [{"pubkey": p} for p in mine]
                    if params[1]["programId"] == CLASSIC else []}
        if method == "getMultipleAccounts":
            return {"value": [_plain(10**9) if a == owner else _acct(owner)
                              for a in params[0]]}
        if method == "simulateTransaction":
            asked = params[1]["accounts"]["addresses"]
            assert len(asked) <= 5
            sims.append(asked)
            return {"value": {"err": None, "accounts": [
                _plain(10**9 - 5_000) if a == owner else
                _acct(owner, delegate=ATTACKER) if a == last else _acct(owner)
                for a in asked]}}
        raise AssertionError(method)

    deltas = sti.simulate(raw, owner=owner, rpc=_rpc)
    assert len(sims) == 2
    assert deltas.ok is True
    assert ("delegate", MINT, ATTACKER) in deltas.authority_grants


def test_an_unenumerable_wallet_refuses_rather_than_narrowing():
    kp = Keypair()
    raw, owner = _tx([(JUP, b"\x00", [(str(kp.pubkey()), True, True)])], kp)

    def _rpc(method, params):
        if method == "getTokenAccountsByOwner":
            raise RuntimeError("rpc down")
        raise AssertionError(method)

    deltas = sti.simulate(raw, owner=owner, rpc=_rpc)
    assert deltas.ok is False
    assert "observation set is incomplete" in deltas.reason


def test_a_short_simulation_answer_refuses():
    kp = Keypair()
    raw, owner = _tx([(JUP, b"\x00", [(str(kp.pubkey()), True, True)])], kp)
    other = str(Pubkey.new_unique())

    def _rpc(method, params):
        if method == "getTokenAccountsByOwner":
            return {"value": [{"pubkey": other}]}
        if method == "getMultipleAccounts":
            return {"value": [None, None]}
        if method == "simulateTransaction":
            return {"value": {"err": None, "accounts": [None]}}
        raise AssertionError(method)

    assert sti.simulate(raw, owner=owner, rpc=_rpc).ok is False


# -- CR-H04: an account the transaction CREATES runs the taxonomy ------------

OWNER = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
NEW = "6ASf5EcmiEXZQFsx1nqvBiYRUwjRW4wCVLxTdxHtnQhU"


def _sim(pre, post):
    return {"value": {"err": None, "accounts": post}, "_pre": pre}


@pytest.mark.parametrize("field,kind", [("delegate", "delegate"),
                                        ("closeAuthority", "close_authority")])
def test_a_created_account_with_a_grant_is_flagged(field, kind):
    d = ss.parse_deltas(_sim([None], [_acct(OWNER, **{field: ATTACKER})]),
                        owner=OWNER, owned_pubkeys=[NEW], ours=[NEW])
    assert (kind, MINT, ATTACKER) in d.authority_grants


def test_a_created_account_that_ends_foreign_owned_is_flagged():
    """SetAuthority(AccountOwner) on the new ATA made the inflow vanish."""
    d = ss.parse_deltas(_sim([None], [_acct(ATTACKER, amount="5000")]),
                        owner=OWNER, owned_pubkeys=[NEW], ours=[OWNER],
                        created=[NEW])
    assert ("owner_changed", MINT, ATTACKER) in d.authority_grants


def test_a_created_mint_is_not_an_owner_change():
    """A mint has no owner field; the SPL deploy creates one."""
    mint = {"lamports": 3_000_000, "owner": CLASSIC,
            "data": {"parsed": {"type": "mint", "info": {
                "decimals": 6, "supply": "1", "mintAuthority": None}}}}
    d = ss.parse_deltas(_sim([None], [mint]), owner=OWNER,
                        owned_pubkeys=[NEW], ours=[OWNER], created=[NEW])
    assert d.authority_grants == ()


# -- CR-L07: a wider re-approve to the same delegate --------------------------

def test_a_larger_delegated_amount_to_the_same_delegate_is_flagged():
    before = _acct(OWNER, delegate=ATTACKER, delegatedAmount={"amount": "1"})
    after = _acct(OWNER, delegate=ATTACKER,
                  delegatedAmount={"amount": "1000000000"})
    d = ss.parse_deltas(_sim([before], [after]), owner=OWNER,
                        owned_pubkeys=[NEW], ours=[NEW])
    assert ("delegate", MINT, ATTACKER) in d.authority_grants


def test_an_unchanged_delegate_still_grants_nothing():
    same = _acct(OWNER, delegate=ATTACKER, delegatedAmount={"amount": "5"})
    d = ss.parse_deltas(_sim([same], [same]), owner=OWNER,
                        owned_pubkeys=[NEW], ours=[NEW])
    assert d.authority_grants == ()


# -- CR-L06: the native outflow the fee and our own rent do not explain -------

def test_native_excess_is_what_fee_and_retained_rent_do_not_explain():
    pre = [_plain(1_000_000_000), None]
    post = [_plain(1_000_000_000 - 5_000 - 2_039_280 - 7_000_000),
            _acct(OWNER, lamports=2_039_280)]
    d = ss.parse_deltas(_sim(pre, post), owner=OWNER,
                        owned_pubkeys=[OWNER, NEW], ours=[OWNER, NEW],
                        fee_lamports=5_000)
    assert d.retained_rent_lamports == 2_039_280
    assert d.native_excess == 7_000_000


def test_native_excess_is_unknown_without_a_fee():
    d = ss.parse_deltas(_sim([_plain(10)], [_plain(5)]), owner=OWNER,
                        owned_pubkeys=[OWNER], ours=[OWNER])
    assert d.native_excess is None
