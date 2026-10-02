"""CR-M05: the Solana rail refuses a durable-nonce transaction before it
journals or contacts the RPC — a nonce transaction never expires."""
import struct

import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.wallet.solana_rail import SolanaBroadcastError, SolanaRail


@pytest.fixture(autouse=True)
def isolated_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))


class _Signer:
    address = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"


def _signed(first_tag):
    kp = Keypair()
    system = Pubkey.from_string("11111111111111111111111111111111")
    ix = Instruction(system, struct.pack("<I", first_tag),
                     [AccountMeta(Pubkey.new_unique(), False, True),
                      AccountMeta(kp.pubkey(), True, True)])
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    tx = VersionedTransaction(msg, [kp])
    return bytes(tx), str(tx.signatures[0])


def test_a_durable_nonce_transaction_is_never_sent():
    raw, _ = _signed(4)                    # AdvanceNonceAccount
    rail = SolanaRail(signer=_Signer(),
                      rpc=lambda *a: pytest.fail("must not contact the RPC"))
    with pytest.raises(SolanaBroadcastError, match="durable-nonce"):
        rail.send_raw(raw)


def test_a_recent_blockhash_transaction_still_sends():
    raw, signature = _signed(2)            # Transfer
    rail = SolanaRail(signer=_Signer(), rpc=lambda m, p: signature)
    assert rail.send_raw(raw) == signature
