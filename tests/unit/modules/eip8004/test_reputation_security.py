"""F3 (P0-1b / N3): ERC-8004 feedback auth must be fail-CLOSED.

Before: create_feedback_auth fell back to a "0x" placeholder signature, and
verify_feedback_auth returned True for ANY non-"0x" signature (accept-any). With
the unauthenticated /reputation/authorize endpoint this was an agent-key signing
oracle + a reputation-poisoning accept-any hole.
"""
import time
import pytest

from modules.eip8004.reputation import ReputationManager
from modules.eip8004.models import FeedbackAuth

_KEY = "0x" + "0" * 63 + "1"
_AGENT_WALLET = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"  # address of _KEY
_OTHER_KEY = "0x" + "0" * 63 + "2"
_CLIENT = "0x" + "2" * 40
_OTHER_CLIENT = "0x" + "3" * 40


def _mgr(monkeypatch, with_key=True):
    env = {
        "EIP8004_AGENT_ID": "42",
        "EIP8004_AGENT_WALLET": _AGENT_WALLET,
        "EIP8004_CHAIN_ID": "8453",
        "EIP8004_REPUTATION_REGISTRY": "0x" + "1" * 40,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if with_key:
        monkeypatch.setenv("EIP8004_AGENT_PRIVATE_KEY", _KEY)
    else:
        monkeypatch.delenv("EIP8004_AGENT_PRIVATE_KEY", raising=False)
    return ReputationManager()


@pytest.mark.asyncio
async def test_signed_auth_round_trips(monkeypatch):
    mgr = _mgr(monkeypatch)
    auth = await mgr.create_feedback_auth(client_address=_CLIENT)
    assert auth.signature and auth.signature != "0x"
    assert mgr.verify_feedback_auth(auth) is True


@pytest.mark.asyncio
async def test_tampered_client_address_rejected(monkeypatch):
    mgr = _mgr(monkeypatch)
    auth = await mgr.create_feedback_auth(client_address=_CLIENT)
    forged = auth.model_copy(update={"clientAddress": _OTHER_CLIENT})
    assert mgr.verify_feedback_auth(forged) is False


def test_placeholder_signature_rejected(monkeypatch):
    mgr = _mgr(monkeypatch)
    auth = FeedbackAuth(
        agentId=42, clientAddress=_CLIENT,
        expiresAt=int(time.time()) + 1000, nonce="n", signature="0x",
    )
    assert mgr.verify_feedback_auth(auth) is False


def test_garbage_signature_rejected(monkeypatch):
    mgr = _mgr(monkeypatch)
    auth = FeedbackAuth(
        agentId=42, clientAddress=_CLIENT,
        expiresAt=int(time.time()) + 1000, nonce="n", signature="0xdeadbeef",
    )
    assert mgr.verify_feedback_auth(auth) is False


def test_expired_auth_rejected(monkeypatch):
    mgr = _mgr(monkeypatch)
    auth = FeedbackAuth(
        agentId=42, clientAddress=_CLIENT,
        expiresAt=int(time.time()) - 1, nonce="n", signature="0x" + "ab" * 65,
    )
    assert mgr.verify_feedback_auth(auth) is False


@pytest.mark.asyncio
async def test_create_without_private_key_raises(monkeypatch):
    """No silent '0x' placeholder — fail closed."""
    mgr = _mgr(monkeypatch, with_key=False)
    with pytest.raises(ValueError):
        await mgr.create_feedback_auth(client_address=_CLIENT)
