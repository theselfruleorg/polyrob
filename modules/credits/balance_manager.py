"""Credit balance manager for managing user credit balances."""

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class CreditBalanceManager:
    """Manage user credit balances."""

    def __init__(self, db, tier_manager=None):
        """
        Initialize credit balance manager.

        Args:
            db: Database manager instance
            tier_manager: TierManager instance (optional)
        """
        self.db = db
        self.tier_manager = tier_manager
        self.logger = logging.getLogger('credits.balance_manager')

    async def get_balance(self, user_id: str) -> dict:
        """Get user's credit balance."""

        result = await self.db.fetch_one("""
            SELECT balance, lifetime_earned, lifetime_spent
            FROM user_credits
            WHERE user_id = ?
        """, (user_id,))

        if not result:
            # User must exist (payment_endpoints ensures this now)
            # Initialize balance
            await self.db.execute("""
                INSERT INTO user_credits (user_id, balance)
                VALUES (?, 0)
            """, (user_id,))
            return {"balance": 0, "lifetime_earned": 0, "lifetime_spent": 0}

        return dict(result)

    async def has_sufficient_balance(self, user_id: str, amount: int) -> bool:
        """Check if user has enough credits."""

        balance = await self.get_balance(user_id)
        return balance['balance'] >= amount

    async def deduct_credits(self, user_id: str, amount: int,
                             reason: str, session_id: str = None) -> bool:
        """
        Deduct credits from user balance.

        Uses atomic UPDATE with WHERE clause to prevent race conditions.
        Wrapped in transaction for consistency between balance update and transaction log.

        Args:
            user_id: User ID
            amount: Amount to deduct
            reason: Reason for deduction
            session_id: Optional session ID

        Returns:
            True if successful, False if insufficient balance
        """
        # Ensure user exists (creates with 0 balance if not)
        await self.get_balance(user_id)

        try:
            # Start transaction for atomic operation
            await self.db.connection.begin_transaction()

            try:
                # SECURITY FIX: Atomic UPDATE with balance check in WHERE clause
                # This prevents TOCTOU race condition where two concurrent requests
                # could both read the same balance before either deducts
                cursor = await self.db.execute("""
                    UPDATE user_credits
                    SET balance = balance - ?,
                        lifetime_spent = lifetime_spent + ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND balance >= ?
                """, (amount, amount, user_id, amount))

                # Check if update succeeded (rowcount > 0 means balance was sufficient)
                if cursor.rowcount == 0:
                    # No rows updated - insufficient balance
                    await self.db.connection.rollback()

                    # Get current balance for logging
                    balance_info = await self.get_balance(user_id)
                    self.logger.warning(
                        f"Insufficient credits for {user_id}: need {amount}, have {balance_info['balance']}"
                    )
                    return False

                # Get new balance for transaction log
                balance_info = await self.get_balance(user_id)
                new_balance = balance_info['balance']
                # Calculate what balance was before deduction
                balance_before = new_balance + amount

                # Record transaction (within same transaction)
                await self.db.execute("""
                    INSERT INTO credit_transactions (
                        user_id, amount, transaction_type, reason,
                        session_id, balance_before, balance_after, timestamp
                    ) VALUES (?, ?, 'usage', ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (user_id, -amount, reason, session_id, balance_before, new_balance))

                # Commit transaction
                await self.db.connection.commit()

                self.logger.info(f"Deducted {amount} credits from {user_id}: {reason}")
                return True

            except (asyncio.CancelledError, Exception) as e:
                # Rollback on any error — INCLUDING asyncio.CancelledError, which
                # derives from BaseException (not Exception) since py3.8. A ticker
                # force-cancel (autonomy-runtime shutdown) or an HTTP-request
                # cancellation mid-transaction would otherwise skip the rollback
                # and leave DatabaseConnection._in_transaction permanently True,
                # poisoning the SHARED bot.db connection for every subsequent
                # write until process restart. Mirrors the exact idiom
                # modules/x402/subscriptions.py::apply_settlement uses (Task 14
                # fix pass 3). Cancellation is re-raised, never swallowed.
                await self.db.connection.rollback()
                raise

        except Exception as e:
            self.logger.error(f"Error deducting credits for {user_id}: {e}")
            raise

    async def add_credits(self, user_id: str, amount: int,
                         reason: str, transaction_type: str = 'purchase') -> bool:
        """
        Add credits to user balance.

        Args:
            user_id: User ID
            amount: Amount to add
            reason: Reason for addition
            transaction_type: Type ('purchase', 'allowance', 'refund', 'admin_grant')

        Returns:
            True if successful
        """

        # Ensure the row exists (creates with 0 balance if not) so the atomic
        # UPDATE below actually matches a row.
        await self.get_balance(user_id)

        # Join an already-open transaction instead of nesting: a caller like
        # DepositMonitor._process_deposit wraps credit + dedup-row in ONE outer
        # transaction, and a nested BEGIN raises (while an inner COMMIT would
        # end the outer transaction early, breaking its rollback guarantee).
        owns_tx = not await self.db.connection.in_transaction()
        try:
            if owns_tx:
                await self.db.connection.begin_transaction()
            try:
                # SECURITY FIX (C2): atomic relative UPDATE, mirroring deduct_credits.
                # The previous read-modify-write (get_balance -> SET balance=<abs>)
                # let two concurrent grants both read the same stale balance and the
                # later absolute write clobber the earlier one — a silently lost grant.
                await self.db.execute("""
                    UPDATE user_credits
                    SET balance = balance + ?,
                        lifetime_earned = lifetime_earned + ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (amount, amount, user_id))

                # Read the authoritative post-update balance for the ledger row.
                balance_info = await self.get_balance(user_id)
                new_balance = balance_info['balance']
                balance_before = new_balance - amount

                await self.db.execute("""
                    INSERT INTO credit_transactions (
                        user_id, amount, transaction_type, reason,
                        balance_before, balance_after, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (user_id, amount, transaction_type, reason, balance_before, new_balance))

                if owns_tx:
                    await self.db.connection.commit()
            except (asyncio.CancelledError, Exception):
                # Roll back on any error INCLUDING asyncio.CancelledError (see the
                # twin comment in deduct_credits): a cancellation mid-transaction
                # must never bypass the rollback and leak _in_transaction=True on
                # the shared bot.db connection.
                if owns_tx:
                    await self.db.connection.rollback()
                raise
        except Exception as e:
            self.logger.error(f"Error adding credits for {user_id}: {e}")
            raise

        self.logger.info(f"Added {amount} credits to {user_id}: {reason} ({transaction_type})")

        return True

    async def get_transaction_history(self, user_id: str, limit: int = 50) -> list:
        """Get user's credit transaction history."""

        results = await self.db.fetch_all("""
            SELECT
                amount,
                transaction_type,
                reason,
                session_id,
                balance_before,
                balance_after,
                timestamp
            FROM credit_transactions
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (user_id, limit))

        return [dict(row) for row in results]

    async def get_monthly_stats(self, user_id: str) -> dict:
        """Get monthly usage statistics."""

        # Get this month's usage
        month_usage = await self.db.fetch_one("""
            SELECT COALESCE(SUM(ABS(amount)), 0) as total
            FROM credit_transactions
            WHERE user_id = ?
                AND transaction_type = 'usage'
                AND timestamp >= date('now', 'start of month')
        """, (user_id,))

        # Get this month's purchases
        month_purchased = await self.db.fetch_one("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM credit_transactions
            WHERE user_id = ?
                AND transaction_type IN ('purchase', 'allowance')
                AND timestamp >= date('now', 'start of month')
        """, (user_id,))

        return {
            "month_spent": month_usage['total'] if month_usage else 0,
            "month_earned": month_purchased['total'] if month_purchased else 0
        }
