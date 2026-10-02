"""C1 (polyrob-desk handoff-core) — the remote signer signs account journal entries, and nothing else.

With ``WALLET_SIGNER=remote`` the agent holds no key, so before this op no journal entry was
ever written. ``journal.sign`` signs EIP-191 over EXACTLY one ``account_journal`` template with
the operational EVM key (the NFT's owner); any other bytes refuse in the agent AND in the signer.
"""
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from core.signer import protocol
from core.signer.remote import RemoteEvmSigner
from core.wallet.account_journal import journal_payload
from core.wallet.agent_wallet import WalletSigningUnavailable
from tests.unit.core.signer.conftest import LoopbackClient
from tests.unit.core.signer.test_066_p2_signer_schemas import _ok, _refused

ACCOUNT = "0xe0f47c083b28c76124129786cbd02a4489b86ec4"


def _payload(**kw):
    base = dict(account=ACCOUNT, chain=4663, seq=0, kind="entry", text_sha256="ab" * 32,
                prev="genesis")
    base.update(kw)
    return journal_payload(**base)


def _recover(data, sig):
    return Account.recover_message(encode_defunct(data), signature=sig).lower()


def test_the_signer_signs_a_journal_template_with_the_operational_key(rig):
    data = _payload()
    out = _ok(rig.call("journal.sign", {"message": data.decode()}))
    owner = rig.service.wallet.operational_signer().address
    assert out["address"].lower() == owner.lower()
    assert _recover(data, out["signature"]) == owner.lower()


@pytest.mark.parametrize("message", [
    "hello",
    _payload().decode() + "\n",                         # one byte past the template
    _payload().decode().replace("agent account", "evil account"),   # an unpinned prefix
    "0x" + "ab" * 32,                                     # a raw digest
    None,
])
def test_the_signer_refuses_anything_but_the_template(rig, message):
    _refused(rig.call("journal.sign", {"message": message}), protocol.UNKNOWN_SHAPE)


def test_the_signer_refuses_extra_keys(rig):
    _refused(rig.call("journal.sign", {"message": _payload().decode(), "digest": "0x00"}),
             protocol.UNKNOWN_SHAPE)


def test_a_paused_signer_signs_no_journal(rig):
    assert rig.call("pause.set", {"paused": True}, uid=0)["ok"]
    _refused(rig.call("journal.sign", {"message": _payload().decode()}), protocol.PAUSED)


def _remote(rig, *, verified=True):
    address = rig.service.wallet.operational_signer().address
    client = LoopbackClient(rig.service)
    return RemoteEvmSigner(client, address, "base", verified_fn=lambda: verified), client


def test_the_agent_side_remote_signer_signs_a_journal_entry(rig):
    signer, client = _remote(rig)
    data = _payload(seq=3, kind="exit", prev="cd" * 32)
    sig = signer.sign_message(data)
    assert _recover(data, sig) == signer.address.lower()
    assert [op for op, _ in client.calls] == ["journal.sign"]


@pytest.mark.parametrize("data", [b"hello", b"", _payload() + b"x"])
def test_the_agent_side_refuses_other_bytes_without_asking_the_signer(rig, data):
    signer, client = _remote(rig)
    with pytest.raises(WalletSigningUnavailable):
        signer.sign_message(data)
    assert client.calls == []


def test_an_unverified_remote_signer_signs_no_journal(rig):
    signer, client = _remote(rig, verified=False)
    with pytest.raises(WalletSigningUnavailable, match="VERIFIED"):
        signer.sign_message(_payload())
    assert client.calls == []


def test_a_journal_entry_signs_on_both_signer_paths(rig):
    """``nft_account.sign_entry`` — the one caller — with the local key and with the remote one."""
    from core.wallet import nft_account
    local = rig.service.wallet.operational_signer()
    remote, _ = _remote(rig)
    for signer in (local, remote):
        entry = nft_account.build_entry(prior=[], account=ACCOUNT, chain_id=4663, kind="note",
                                        text="hello", owner=signer.address)
        signed = nft_account.sign_entry(entry, signer)
        assert nft_account.recover_owner(signed) == signer.address.lower()
