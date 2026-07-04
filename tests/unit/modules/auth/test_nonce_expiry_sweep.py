"""E2 — cleanup_expired_nonces deletes only expired rows. Pre-existing method,
previously untested and never called anywhere in the codebase."""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.auth_tables import AuthTables
from modules.auth.siwe_auth import SIWEAuthenticator

WALLET = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"


@pytest.mark.asyncio
async def test_cleanup_expired_nonces_deletes_only_expired(tmp_path):
    db = DatabaseConnection(tmp_path / "auth.db")
    await db.connect()
    await AuthTables(db).create_tables()
    auth = SIWEAuthenticator(db)

    fresh = await auth.generate_nonce(WALLET, chain_id=1)  # expires_at = now + 5min
    await db.execute(
        "INSERT INTO auth_nonces (wallet_address, nonce, chain_id, expires_at) "
        "VALUES (?, 'stale-nonce', 1, datetime('now', '-1 hour'))",
        (WALLET.lower(),),
    )

    await auth.cleanup_expired_nonces()

    remaining = await db.fetch_all(
        "SELECT nonce FROM auth_nonces WHERE wallet_address=?", (WALLET.lower(),)
    )
    remaining_nonces = {r["nonce"] for r in remaining}
    assert fresh in remaining_nonces
    assert "stale-nonce" not in remaining_nonces
