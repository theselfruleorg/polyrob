"""C2: add_credits did read-modify-write (get_balance -> compute -> SET balance=<abs>).
Two concurrent grants (crypto deposit + admin grant, or a double-submitted webhook)
both read the same stale balance and the later absolute write clobbers the earlier —
one grant is silently lost. Its sibling deduct_credits is atomic; add_credits must be
too: SET balance = balance + ?.
"""
import asyncio

from modules.credits.balance_manager import CreditBalanceManager


class _Cursor:
    rowcount = 1


class _Conn:
    async def in_transaction(self):
        return False

    async def begin_transaction(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class _DB:
    """Records executed SQL; fetch_one returns a fixed post-update balance."""

    def __init__(self, balance):
        self.executed = []
        self._balance = balance
        self.connection = _Conn()

    async def fetch_one(self, sql, params=()):
        return {"balance": self._balance, "lifetime_earned": 0, "lifetime_spent": 0}

    async def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), tuple(params)))
        return _Cursor()


def test_add_credits_uses_atomic_relative_update():
    db = _DB(balance=250)  # the DB's authoritative post-update balance
    mgr = CreditBalanceManager(db)

    ok = asyncio.run(mgr.add_credits("u1", 100, "deposit", "purchase"))
    assert ok is True

    updates = [(s, p) for (s, p) in db.executed if s.startswith("UPDATE user_credits")]
    assert updates, "no balance UPDATE issued"
    sql, params = updates[0]
    # Atomic increment, never an absolute stale-read write.
    assert "balance = balance + ?" in sql, f"non-atomic update: {sql}"
    assert "SET balance = ?," not in sql

    # Ledger row derives balance_before/after from the authoritative post-update read.
    inserts = [(s, p) for (s, p) in db.executed if "INSERT INTO credit_transactions" in s]
    assert inserts, "no ledger row written"
    _, iparams = inserts[0]
    assert iparams[0] == "u1"
    assert iparams[1] == 100
    assert iparams[-2] == 150  # balance_before = 250 - 100
    assert iparams[-1] == 250  # balance_after (authoritative)
