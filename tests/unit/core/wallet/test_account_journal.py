"""The account journal template (050 §6.6; 069 v4 §5 rule 6).

069 v4: a journal entry is a PLAIN owner-key EIP-191 signature over the template. There is no
binding and no signer lock-down any more: the local signer signs any message and typed data.
"""
from unittest import mock

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from core.wallet import account_journal
from core.wallet.signer import LocalEoaSigner

KEY = bytes.fromhex("11" * 32)
ACCOUNT = "0xe0f47c083b28c76124129786cbd02a4489b86ec4"


def _payload(**kw):
    base = dict(account=ACCOUNT, chain=4663, seq=0, kind="handover", text_sha256="ab" * 32, prev="genesis")
    base.update(kw)
    return account_journal.journal_payload(**base)


def _payload_with_prefix(prefix):
    with mock.patch.object(account_journal, "allowed_journal_prefixes", lambda: ("agent", prefix)):
        return _payload(prefix=prefix)


def test_the_template_round_trips_and_nothing_else_matches():
    p = _payload()
    assert account_journal.is_journal_template(p)
    assert p.startswith(b"agent account journal\naccount=0x")
    assert not account_journal.is_journal_template(p + b"\nextra=1")
    assert not account_journal.is_journal_template(b"Sign in to OpenSea")
    assert not account_journal.is_journal_template(p.replace(b"kind=handover", b"kind=approve"))
    with pytest.raises(ValueError):
        _payload(kind="approve")


def test_only_the_configured_prefixes_match():
    """069: the default prefix is `agent`; a pinned profile may add its own, nothing looser."""
    p = _payload()
    custom = _payload_with_prefix("POLYROB")
    assert custom.startswith(b"POLYROB account journal\naccount=0x")
    assert not account_journal.is_journal_template(custom, prefixes=("agent",))
    assert account_journal.is_journal_template(custom, prefixes=("agent", "POLYROB"))
    assert not account_journal.is_journal_template(p.replace(b"agent account", b"Agent account"))
    with pytest.raises(ValueError):
        _payload(prefix="bad\nprefix")
    with pytest.raises(ValueError, match="pinned"):
        _payload(prefix="Unpinned")      # a valid prefix no profile carries is not signable


def test_the_owner_key_signs_the_journal_as_a_plain_signature():
    signer = LocalEoaSigner(KEY)
    p = _payload()
    sig = signer.sign_message(p)
    assert Account.recover_message(encode_defunct(p), signature=sig) == signer.address


def test_the_local_signer_has_no_lock_down():
    """No binding: any message and typed data sign, and the raw account is available."""
    signer = LocalEoaSigner(KEY)
    assert signer.sign_message(b"hello")
    domain = {"name": "X", "version": "1", "chainId": 1,
              "verifyingContract": "0x" + "00" * 19 + "01"}
    types = {"M": [{"name": "a", "type": "uint256"}]}
    assert signer.sign_typed_data(domain, types, {"a": 1})
    assert signer.account.address == signer.address
