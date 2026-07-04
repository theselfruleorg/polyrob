"""F9 (P1-4): SIWE endpoints must validate + checksum-normalize wallet_address.

Unvalidated addresses flowed into the JWT `sub` / user_id / admin checks, and
casing differences produced fragmented identities for the same wallet.
"""
import pytest
from pydantic import ValidationError

from api.auth_endpoints import NonceRequest, VerifyRequest

_LOWER = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"
_CHECKSUM = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"


@pytest.mark.parametrize("bad", ["", "0xabc", "abc123", "0x" + "z" * 40, "0x" + "1" * 39])
def test_nonce_rejects_malformed_address(bad):
    with pytest.raises(ValidationError):
        NonceRequest(wallet_address=bad)


def test_nonce_normalizes_to_checksum():
    assert NonceRequest(wallet_address=_LOWER).wallet_address == _CHECKSUM
    assert NonceRequest(wallet_address=_CHECKSUM).wallet_address == _CHECKSUM


def test_verify_normalizes_to_checksum():
    v = VerifyRequest(wallet_address=_LOWER, message="m", signature="s", nonce="n")
    assert v.wallet_address == _CHECKSUM


def test_verify_rejects_malformed():
    with pytest.raises(ValidationError):
        VerifyRequest(wallet_address="0xnope", message="m", signature="s", nonce="n")
