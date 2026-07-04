"""E3 — AuditLogger needs a dedicated admin-wallet-auth event so admin-privilege
session grants leave a queryable trail (today: logger.info only, unqueryable)."""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.audit_log import AuditLogger

WALLET = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"


@pytest.mark.asyncio
async def test_log_admin_wallet_auth_writes_a_queryable_row(tmp_path):
    db = DatabaseConnection(tmp_path / "audit.db")
    await db.connect()
    audit = AuditLogger(db)
    await audit.create_table()

    await audit.log_admin_wallet_auth(wallet_address=WALLET, user_id="u-admin-1")

    rows = await audit.get_recent_events(event_type=AuditLogger.EVENT_ADMIN_WALLET_AUTH)
    assert len(rows) == 1
    assert rows[0]["actor_wallet"] == WALLET
    assert rows[0]["target_id"] == "u-admin-1"
