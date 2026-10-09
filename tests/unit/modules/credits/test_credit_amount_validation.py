import pytest
from modules.credits.balance_manager import CreditBalanceManager


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [-500, 0, 1_000_000_001, True, 0.5, float("nan"), float("inf")])
@pytest.mark.parametrize("method", ["add_credits", "deduct_credits"])
async def test_invalid_credit_change_never_touches_database(amount, method):
    manager = CreditBalanceManager(None)
    with pytest.raises(ValueError):
        await getattr(manager, method)("tenant", amount, "test")


@pytest.mark.asyncio
async def test_unpaid_call_blocks_reusing_a_small_balance(tmp_path):
    from modules.database.connection import DatabaseConnection
    db = DatabaseConnection(tmp_path / "billing.db")
    await db.connect()
    try:
        await db.execute("CREATE TABLE user_credits (user_id TEXT, balance INTEGER, lifetime_earned INTEGER, lifetime_spent INTEGER)")
        await db.execute("CREATE TABLE billing_failures (id INTEGER, user_id TEXT, status TEXT)")
        await db.execute("INSERT INTO user_credits VALUES ('tenant', 1, 1, 0)")
        manager = CreditBalanceManager(db)
        assert await manager.has_sufficient_balance("tenant", 1)
        await db.execute("INSERT INTO billing_failures VALUES (1, 'tenant', 'pending')")
        assert not await manager.has_sufficient_balance("tenant", 1)
        await db.execute("UPDATE billing_failures SET status = 'resolved'")
        assert await manager.has_sufficient_balance("tenant", 1)
    finally:
        await db.close()
