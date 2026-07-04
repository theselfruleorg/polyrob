from eth_account import Account
from core.wallet.signer import LocalEoaSigner

# Deterministic well-known test key (NOT a real funded key)
TEST_KEY = bytes.fromhex("4c0883a69102937d6231471b5dbb6204fe512961708279f2e3e8a5d4b8e3e3e3")


def test_address_matches_eth_account():
    signer = LocalEoaSigner(TEST_KEY)
    assert signer.address == Account.from_key(TEST_KEY).address


def test_sign_message_is_recoverable():
    signer = LocalEoaSigner(TEST_KEY)
    sig = signer.sign_message(b"hello")
    assert isinstance(sig, str) and sig.startswith("0x") and len(sig) == 132


def test_sign_typed_data_returns_hex_signature():
    signer = LocalEoaSigner(TEST_KEY)
    domain = {"name": "Test", "version": "1", "chainId": 8453, "verifyingContract": "0x" + "00" * 20}
    types = {"Mail": [{"name": "contents", "type": "string"}]}
    message = {"contents": "hi"}
    sig = signer.sign_typed_data(domain, types, message)
    assert sig.startswith("0x") and len(sig) == 132


def test_signer_never_exposes_private_key():
    signer = LocalEoaSigner(TEST_KEY)
    leaked = TEST_KEY.hex()
    for attr in ("address",):
        assert leaked not in str(getattr(signer, attr)).lower()
    # repr must not leak the key
    assert leaked not in repr(signer).lower()
