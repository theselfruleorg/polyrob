"""E1 — SIWE chain-ID replay: a nonce issued for one chain must not validate
against a signed message that declares a different Chain ID. Today auth_nonces
has no chain_id column at all, so nothing is even persisted to check against.
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.auth_tables import AuthTables
from modules.auth.siwe_auth import SIWEAuthenticator

WALLET = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"


async def _authenticator(tmp_path):
    db = DatabaseConnection(tmp_path / "auth.db")
    await db.connect()
    await AuthTables(db).create_tables()
    return SIWEAuthenticator(db), db


@pytest.mark.asyncio
async def test_nonce_row_persists_the_issuing_chain_id(tmp_path):
    auth, db = await _authenticator(tmp_path)
    nonce = await auth.generate_nonce(WALLET, chain_id=8453)
    row = await db.fetch_one(
        "SELECT chain_id FROM auth_nonces WHERE wallet_address=? AND nonce=?",
        (WALLET.lower(), nonce),
    )
    assert row is not None
    assert row["chain_id"] == 8453


@pytest.mark.asyncio
async def test_verify_nonce_rejects_chain_id_mismatch(tmp_path):
    auth, _db = await _authenticator(tmp_path)
    nonce = await auth.generate_nonce(WALLET, chain_id=1)  # issued for Ethereum mainnet
    # Attacker resubmits the SAME nonce but a message claiming a different chain.
    assert await auth._verify_nonce(WALLET, nonce, chain_id=137) is False


@pytest.mark.asyncio
async def test_verify_nonce_accepts_matching_chain_id(tmp_path):
    auth, _db = await _authenticator(tmp_path)
    nonce = await auth.generate_nonce(WALLET, chain_id=1)
    assert await auth._verify_nonce(WALLET, nonce, chain_id=1) is True


def test_extract_chain_id_parses_the_siwe_message_line():
    msg = "example.com wants you to sign in...\n\nChain ID: 8453\nNonce: abc"
    assert SIWEAuthenticator._extract_chain_id(msg) == 8453


def test_extract_chain_id_returns_none_when_absent():
    assert SIWEAuthenticator._extract_chain_id("no chain line here") is None


@pytest.mark.asyncio
async def test_verify_nonce_rejects_omitted_chain_id_line(tmp_path):
    """Fail-closed regression: a chain-bound nonce (row chain_id=1) must be
    REJECTED when the submitted message simply omits the `Chain ID:` line
    (parses to chain_id=None). Pre-fix, `if chain_id is not None` short-
    circuited on this case and returned True — a straight bypass of the E1
    chain-binding fix requiring no more capability than the replay it targets.
    """
    auth, _db = await _authenticator(tmp_path)
    nonce = await auth.generate_nonce(WALLET, chain_id=1)  # issued for Ethereum mainnet
    message_without_chain_line = (
        "example.com wants you to sign in with your Ethereum account:\n"
        f"{WALLET}\n\nSign in\n\nURI: https://example.com\nVersion: 1\n"
        f"Nonce: {nonce}\nIssued At: 2026-01-01T00:00:00Z"
    )
    submitted_chain_id = SIWEAuthenticator._extract_chain_id(message_without_chain_line)
    assert submitted_chain_id is None  # sanity: the line really is absent

    assert await auth._verify_nonce(WALLET, nonce, chain_id=submitted_chain_id) is False


@pytest.mark.asyncio
async def test_verify_nonce_allows_legacy_null_chain_row(tmp_path):
    """Grace period: a nonce row persisted before the chain_id migration
    (chain_id IS NULL) must still validate regardless of what chain, if any,
    the submitted message declares — there is nothing to bind against."""
    auth, db = await _authenticator(tmp_path)
    nonce = "legacy-nonce-with-no-chain-binding"
    await db.execute(
        """
        INSERT INTO auth_nonces (wallet_address, nonce, chain_id, expires_at)
        VALUES (?, ?, NULL, datetime('now', '+5 minutes'))
        """,
        (WALLET.lower(), nonce),
    )

    assert await auth._verify_nonce(WALLET, nonce, chain_id=None) is True
    assert await auth._verify_nonce(WALLET, nonce, chain_id=137) is True
