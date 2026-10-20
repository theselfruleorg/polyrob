"""085 W8 solana L1 / L2 / L6 — the wallet account post-state, the undecoded
Token-2022 tags, and a v1 Jupiter route with no amounts."""
import struct
from hashlib import sha256

import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.wallet import solana_tx_inspect as sti
from core.wallet.solana_swap_bounds import SwapBounds, V6, refusal
from core.wallet.solana_tx_inspect import DecodedIx, candidate_atas

JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
T22 = sti.TOKEN_2022_PROGRAM_ID
SYSTEM = sti.SYSTEM_PROGRAM_ID
EVIL = "7WktogJEd2wQ9eH2oWusmcoFTgeYi6rS632UviTBJ2jm"


def _tx(ixs, kp):
    built = [Instruction(Pubkey.from_string(p), data,
                         [AccountMeta(Pubkey.from_string(str(k)), s, w)
                          for k, s, w in metas])
             for p, data, metas in ixs]
    msg = MessageV0.try_compile(kp.pubkey(), built, [], Hash.default())
    return bytes(VersionedTransaction(msg, [kp]))


# -- L1: the wallet's own account must stay a plain System account ----------

def _sim_rpc(owner, post_owner_state):
    def _rpc(method, params):
        if method == "getTokenAccountsByOwner":
            return {"value": []}
        if method == "getMultipleAccounts":
            return {"value": [{"lamports": 10**9, "owner": SYSTEM,
                               "data": ["", "base64"], "executable": False}
                              for _ in params[0]]}
        if method == "simulateTransaction":
            return {"value": {"err": None, "accounts": [
                post_owner_state for _ in params[1]["accounts"]["addresses"]]}}
        raise AssertionError(method)
    return _rpc


def _route_tx():
    kp = Keypair()
    raw = _tx([(JUP, b"\x00", [(str(kp.pubkey()), True, True)])], kp)
    return raw, str(kp.pubkey())


def test_a_normal_post_state_passes():
    raw, owner = _route_tx()
    post = {"lamports": 10**9 - 5_000, "owner": SYSTEM,
            "data": ["", "base64"], "executable": False, "space": 0}
    deltas = sti.simulate(raw, owner=owner, rpc=_sim_rpc(owner, post))
    assert deltas.ok is True
    assert deltas.native_delta == -5_000


@pytest.mark.parametrize("post,word", [
    ({"lamports": 10**9 - 5_000, "owner": EVIL, "data": ["", "base64"]}, "owned by program"),
    ({"lamports": 10**9 - 5_000, "data": ["", "base64"]}, "owned by program"),
    ({"lamports": 10**9 - 5_000, "owner": SYSTEM, "executable": True}, "executable"),
    ({"lamports": 10**9 - 5_000, "owner": SYSTEM, "data": ["AAAA", "base64"]}, "data allocated"),
    ({"lamports": 10**9 - 5_000, "owner": SYSTEM, "data": ["", "base64"], "space": 8},
     "data allocated"),
])
def test_a_reassigned_or_allocated_wallet_account_refuses(post, word):
    """A CPI Assign/Allocate on our signer leaves the lamports unchanged."""
    raw, owner = _route_tx()
    deltas = sti.simulate(raw, owner=owner, rpc=_sim_rpc(owner, post))
    assert deltas.ok is False
    assert word in deltas.reason


def test_the_wallet_account_rule_unit():
    assert sti.wallet_account_refusal(None) is None
    assert sti.wallet_account_refusal({"owner": SYSTEM, "lamports": 1}) is None
    assert sti.wallet_account_refusal({"owner": EVIL, "lamports": 1})


# -- L2: Token-2022 WithdrawExcessLamports (38) and Reallocate (29) ---------

@pytest.mark.parametrize("tag,name", [(38, "WithdrawExcessLamports"), (29, "Reallocate")])
def test_a_t22_tag_on_an_existing_account_refuses(tag, name):
    kp = Keypair()
    owner = str(kp.pubkey())
    victim = str(Pubkey.new_unique())
    raw = _tx([(JUP, b"\x00", [(owner, True, True)]),
               (T22, bytes([tag]) + b"\x00\x00",
                [(victim, False, True), (EVIL, False, True), (owner, True, False)])], kp)
    got = sti.inspect_transaction(raw)
    assert got.ok is False
    assert name in got.reason


def test_a_t22_tag_on_an_account_this_tx_created_is_allowed():
    created = "6ASf5EcmiEXZQFsx1nqvBiYRUwjRW4wCVLxTdxHtnQhU"
    owner = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
    create = DecodedIx(SYSTEM, struct.pack("<I", 0) + struct.pack("<QQ", 2_000_000, 165)
                       + bytes(Pubkey.from_string(T22)), (owner, created))
    realloc = DecodedIx(T22, bytes([29]), (created, owner, SYSTEM, owner))
    assert sti.instruction_refusal([create, realloc], owner=owner) is None


# -- L6: a v1 route that ends right after its plan refuses cleanly ----------

OWNER = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"


def test_a_v1_route_without_amounts_refuses_instead_of_crashing():
    data = sha256(b"global:route").digest()[:8]
    data += struct.pack("<I", 1) + bytes([7]) + bytes([100, 0, 1])  # plan, no amounts
    accounts = (V6, OWNER, V6, candidate_atas(OWNER, WSOL)[0], V6, WSOL)
    why = refusal([DecodedIx(V6, data, accounts)],
                  SwapBounds(USDC, WSOL, 1_000_000, 990_000, 100), owner=OWNER)
    assert why and "truncated" in why
