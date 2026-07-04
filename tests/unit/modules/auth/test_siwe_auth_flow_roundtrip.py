"""E7 — real SIWE auth-flow integration test (nonce -> sign -> verify): the SIWE
half of 'auth-flow integration test (owner login + SIWE)'. The owner-login half
depends on B3 (not yet built) — see the E7 hand-off note in the workstream-E
plan for what to add once B3 lands.
"""
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from modules.database.connection import DatabaseConnection
from modules.database.auth_tables import AuthTables
from modules.auth.siwe_auth import SIWEAuthenticator


async def _authenticator(tmp_path):
    db = DatabaseConnection(tmp_path / "auth.db")
    await db.connect()
    await AuthTables(db).create_tables()
    return SIWEAuthenticator(db)


@pytest.mark.asyncio
async def test_siwe_full_round_trip_nonce_sign_verify(tmp_path):
    auth = await _authenticator(tmp_path)
    acct = Account.create()

    result = await auth.create_siwe_message(
        wallet_address=acct.address, domain="app.example.com",
        uri="https://app.example.com", chain_id=8453,
    )
    signed = Account.sign_message(encode_defunct(text=result["message"]), private_key=acct.key)

    ok = await auth.verify_signature(
        wallet_address=acct.address, message=result["message"],
        signature=signed.signature.hex(), nonce=result["nonce"],
    )
    assert ok is True

    # Nonce is one-time-use: replay must be rejected.
    replay_ok = await auth.verify_signature(
        wallet_address=acct.address, message=result["message"],
        signature=signed.signature.hex(), nonce=result["nonce"],
    )
    assert replay_ok is False


@pytest.mark.asyncio
async def test_siwe_rejects_wrong_signer(tmp_path):
    auth = await _authenticator(tmp_path)
    real_acct = Account.create()
    attacker_acct = Account.create()

    result = await auth.create_siwe_message(
        wallet_address=real_acct.address, domain="app.example.com",
        uri="https://app.example.com",
    )
    # Attacker signs with THEIR OWN key but claims to be real_acct.
    signed = Account.sign_message(encode_defunct(text=result["message"]), private_key=attacker_acct.key)

    ok = await auth.verify_signature(
        wallet_address=real_acct.address, message=result["message"],
        signature=signed.signature.hex(), nonce=result["nonce"],
    )
    assert ok is False
