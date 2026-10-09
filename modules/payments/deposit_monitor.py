"""Deposit monitoring service for automatic credit top-ups.

Monitors blockchain for deposits to user addresses and automatically
credits their accounts.
"""

import asyncio
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from modules.payments.networks import chain_configs, TOKEN_ADDRESSES, ERC20_BALANCEOF_ABI

logger = logging.getLogger(__name__)


class DepositMonitor:
    """Monitor blockchain for deposits and credit user accounts."""

    def __init__(self, db_manager, balance_manager, config, notify_callback=None):
        """Initialize deposit monitor.

        Args:
            db_manager: Database manager instance
            balance_manager: Credit balance manager
            config: Bot configuration
            notify_callback: optional `Callable[[str, str], Optional[Awaitable]]`
                (user_id, message) invoked best-effort after a deposit is
                credited (C8). A callback failure never affects crediting —
                money already moved by the time it's called. When None
                (default), only the durable `user_notifications` DB row is
                written; wiring an actual delivery channel (Telegram/email)
                is the operator's job via this hook, not this module's.
        """
        self.db = db_manager
        self.balance_manager = balance_manager
        self.config = config
        self.notify_callback = notify_callback
        self.logger = logging.getLogger('payments.deposit_monitor')

        # Check interval (seconds)
        self.check_interval = getattr(config, 'deposit_check_interval', 60)

        # Supported chains and tokens (shared SSOT with TreasurySweeper)
        self.chains = chain_configs(config)
        self.token_addresses = TOKEN_ADDRESSES

        # Minimum deposit thresholds (USD)
        self.min_deposit_usd = 5.00

        # Credit rate: $0.01 per credit
        self.credit_rate = 0.01

        # Running flag
        self.running = False
        self._task: Optional[asyncio.Task] = None

        # Lazily-created-once notification table guard (see
        # `_ensure_notifications_table`) — created at most once per monitor
        # instance, not once per deposit.
        self._notifications_table_ready = False

    async def start(self):
        """Start monitoring deposits."""
        if self.running:
            self.logger.warning("Deposit monitor already running")
            return

        await self._ensure_notifications_table()

        self.running = True
        self._task = asyncio.create_task(self._monitor_loop())
        self.logger.info(f"🔍 Deposit monitor started (check interval: {self.check_interval}s)")

    async def stop(self):
        """Stop monitoring deposits."""
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.logger.info("Deposit monitor stopped")

    async def _monitor_loop(self):
        """Main monitoring loop."""
        while self.running:
            try:
                await self._check_all_deposits()
            except Exception as e:
                self.logger.error(f"Error in deposit monitor loop: {type(e).__name__}")

            # Sleep until next check
            await asyncio.sleep(self.check_interval)

    async def _check_all_deposits(self):
        """Check all user addresses for new deposits."""
        try:
            # Get all user deposit addresses that need checking
            addresses = await self.db.fetch_all("""
                SELECT user_id, deposit_address, last_checked
                FROM user_deposit_addresses
                WHERE last_checked IS NULL
                   OR datetime(last_checked) < datetime('now', '-1 minute')
                ORDER BY last_checked ASC NULLS FIRST
                LIMIT 100
            """)

            if not addresses:
                self.logger.debug("No addresses to check")
                return

            self.logger.info(f"Checking {len(addresses)} deposit addresses")

            for addr_row in addresses:
                user_id = addr_row['user_id']
                address = addr_row['deposit_address']

                try:
                    # Check each chain for deposits
                    for chain_name, chain_config in self.chains.items():
                        if not chain_config['rpc_url'] or chain_config.get('testnet', True):
                            continue

                        deposits = await self._check_chain_deposits(
                            address,
                            chain_name,
                            chain_config
                        )

                        # Process any new deposits
                        for deposit in deposits:
                            await self._process_deposit(user_id, deposit)

                    # Update last_checked timestamp
                    await self.db.execute("""
                        UPDATE user_deposit_addresses
                        SET last_checked = datetime('now')
                        WHERE user_id = ?
                    """, (user_id,))

                except Exception as e:
                    self.logger.error(f"Error checking address {address}: {type(e).__name__}")
                    continue

        except Exception as e:
            self.logger.error(f"Error in _check_all_deposits: {type(e).__name__}")

    async def _check_chain_deposits(
        self,
        address: str,
        chain_name: str,
        chain_config: Dict
    ) -> List[Dict]:
        """Check a specific chain for deposits to an address.

        Args:
            address: Deposit address to check
            chain_name: Chain name (polygon, base, arbitrum)
            chain_config: Chain configuration

        Returns:
            List of deposit dictionaries
        """
        deposits = []

        try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(chain_config['rpc_url']))
            actual_chain = await asyncio.to_thread(lambda: w3.eth.chain_id)
            if actual_chain != chain_config['chain_id']:
                raise ValueError('Deposit RPC chain does not match configured chain')

            # Every balance READING is reported, including zero and dust
            # (CR-H08): `_process_deposit` credits only the INCREASE over the
            # last observed balance, so it must also see a DECREASE (a sweep)
            # to lower its mark — otherwise the next deposit below the old
            # high-water mark is never credited. The minimum-deposit floor
            # applies to the increase, in `_process_deposit`.
            # A reversible tip balance must never mint spendable credits.
            block = await asyncio.to_thread(w3.eth.get_block, "finalized")
            block_number = int(block["number"])
            eth_balance = await asyncio.to_thread(w3.eth.get_balance, address, block_number)
            eth_price = None
            if eth_balance > 0:
                try:
                    eth_price = await self._get_eth_price()
                except Exception as e:
                    self.logger.error(
                        f"ETH price oracle unavailable, skipping ETH deposit check "
                        f"for {address} this cycle: {type(e).__name__}"
                    )
            if eth_balance == 0 or eth_price is not None:
                deposits.append({
                    'chain': chain_name,
                    'token_symbol': 'ETH',
                    # Price-INDEPENDENT balance reading (wei): the mark in
                    # `_process_deposit` compares readings, never amount_usd,
                    # which is `eth_price * balance` and moves every tick.
                    'amount': str(eth_balance),
                    'amount_wei': eth_balance,
                    'amount_usd': Decimal(str(eth_price or 0)) * Decimal(eth_balance) / 10**18,
                })

            # Check each token on this chain
            token_addresses = self.token_addresses.get(chain_name, {})
            for token_symbol, token_address in token_addresses.items():
                balance = await self._get_token_balance(
                    w3,
                    address,
                    token_address,
                    block_number,
                )
                if balance is None:
                    continue  # unreadable is not zero: never lower the mark on a failed read

                # Stablecoins are 1:1 with USD
                deposits.append({
                    'chain': chain_name,
                    'token_symbol': token_symbol,
                    'amount': str(balance),
                    'amount_usd': balance,
                })

        except Exception as e:
            self.logger.error("Deposit RPC read failed on %s (%s)", chain_name, type(e).__name__)

        return deposits

    async def _get_token_balance(self, w3, address: str, token_address: str,
                                 block_identifier="finalized") -> Optional[Decimal]:
        """Get ERC20 token balance.

        Args:
            w3: Web3 instance
            address: User address
            token_address: Token contract address

        Returns:
            Token balance as Decimal, or None when the read failed (an
            unreadable balance is not a zero balance — CR-H08).
        """
        try:
            contract = w3.eth.contract(address=token_address, abi=ERC20_BALANCEOF_ABI)
            balance_wei = await asyncio.to_thread(
                contract.functions.balanceOf(address).call, block_identifier=block_identifier)

            # USDC/USDT have 6 decimals
            balance = Decimal(balance_wei) / 10**6

            return balance

        except Exception as e:
            self.logger.debug("Token balance read failed (%s)", type(e).__name__)
            return None

    async def _get_eth_price(self) -> float:
        """Get current ETH price in USD from the live oracle (C8).

        Fail-closed by design (see modules.payments.price_oracle docstring):
        if the oracle call fails, callers must NOT fall back to a stale
        hardcoded number — _check_chain_deposits catches the exception and
        skips ETH-deposit detection for this cycle only (other tokens on the
        same cycle are unaffected).
        """
        from modules.payments.price_oracle import get_eth_price_usd
        return await get_eth_price_usd()

    async def _process_deposit(self, user_id: str, deposit: Dict):
        """Process a detected deposit.

        Args:
            user_id: User ID
            deposit: Deposit information
        """
        if self.chains.get(deposit.get('chain'), {}).get('testnet', True):
            self.logger.warning("Refusing paid credits for an unsupported or test network")
            return
        try:
            # CR-H08: credit only the INCREASE over the last observed balance
            # for this (user, chain, token). The balance reading is
            # price-independent (wei for ETH, the token balance for ERC20s) —
            # NEVER amount_usd, which moves with the live oracle every tick.
            # Crediting the WHOLE balance on every change minted 10 + 10.5 +
            # 11 USDC of credits for 11 USDC received.
            #
            # 'amount' is REQUIRED, never a price-derived fallback — let a
            # producer that forgets it raise loudly.
            deposit_amount = deposit['amount']
            current = Decimal(str(deposit_amount))

            await self._ensure_marks_table()
            previous = await self._last_balance_mark(
                user_id, deposit['chain'], deposit['token_symbol'])
            if (not current.is_finite() or not previous.is_finite()
                    or current < 0 or previous < 0):
                raise ValueError('deposit balances must be finite and nonnegative')

            if current <= previous:
                if current < previous:
                    # Funds left the address (a sweep). Lower the mark so the
                    # next deposit is measured from what is there now.
                    await self._write_balance_mark(
                        user_id, deposit['chain'], deposit['token_symbol'], deposit_amount)
                self.logger.debug(
                    f"No new deposit for {user_id} ({deposit['token_symbol']} "
                    f"on {deposit['chain']}): balance {current} <= mark {previous}")
                return

            # Only the increase is new money. amount_usd prices the WHOLE
            # reading; scale it to the increase.
            full_usd = Decimal(str(deposit['amount_usd']))
            rate = Decimal(str(self.credit_rate))
            if (not all(v.is_finite() for v in (current, previous, full_usd, rate))
                    or full_usd <= 0 or rate <= 0):
                raise ValueError('deposit valuation and credit rate must be finite and positive')
            delta_usd = full_usd * (current - previous) / current
            if delta_usd < Decimal(str(self.min_deposit_usd)):
                # Dust: leave the mark where it is so dust accumulates until
                # the increase crosses the floor, then it credits once.
                return
            credits = int(delta_usd / rate)
            amount_usd = float(delta_usd)

            # Credit the user's balance AND record the crypto_payments
            # dedup row in ONE transaction (money-safety re-review, MEDIUM):
            # crediting and recording used to be two separate autocommitted
            # writes, so a crash / a failed INSERT between them left the
            # credit committed but no dedup row on disk — the next tick would
            # find nothing and re-credit the same deposit. Mirrors the
            # begin_transaction/commit/rollback pattern
            # `CreditBalanceManager.deduct_credits` already uses: either both
            # writes land, or neither does, and a crash mid-transaction rolls
            # back cleanly so the deposit is simply retried once more.
            await self.db.connection.begin_transaction()
            try:
                await self.balance_manager.add_credits(
                    user_id=user_id,
                    amount=credits,
                    reason=f"Crypto deposit: {amount_usd:.2f} USD in {deposit['token_symbol']} on {deposit['chain']}"
                )

                await self.db.execute("""
                    INSERT INTO crypto_payments (
                        user_id, chain, deposit_address, token_symbol,
                        amount, amount_usd, credits_purchased,
                        status, detected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', datetime('now'))
                """, (
                    user_id,
                    deposit['chain'],
                    deposit.get('address', ''),
                    deposit['token_symbol'],
                    deposit_amount,
                    amount_usd,
                    credits
                ))
                await self._write_balance_mark(
                    user_id, deposit['chain'], deposit['token_symbol'], deposit_amount)

                await self.db.connection.commit()
            except Exception:
                await self.db.connection.rollback()
                raise

            self.logger.info(
                f"✅ Processed deposit for {user_id}: "
                f"{amount_usd:.2f} USD ({credits} credits) - "
                f"{deposit['token_symbol']} on {deposit['chain']}"
            )

            await self._notify_deposit_credited(
                user_id, {**deposit, 'amount_usd': amount_usd}, credits)

        except Exception as e:
            self.logger.error(f"Error processing deposit for {user_id}: {type(e).__name__}")

    async def _ensure_marks_table(self):
        """Create the per-address balance mark table once (CR-H08).

        Additive and idempotent (`CREATE TABLE IF NOT EXISTS`): an existing
        database only gains the table.
        """
        if getattr(self, '_marks_table_ready', False):
            return
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS deposit_balance_marks (
                user_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                token_symbol TEXT NOT NULL,
                balance TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, chain, token_symbol)
            )
        """)
        self._marks_table_ready = True

    async def _last_balance_mark(self, user_id: str, chain: str, token_symbol: str) -> Decimal:
        """The last balance reading this monitor accounted for.

        No mark yet (a database from before CR-H08): the balance recorded on
        the newest `crypto_payments` row — the old code stored the whole
        balance reading there — so a monitor upgraded over an un-swept,
        already-credited address does not credit it a second time.
        """
        row = await self.db.fetch_one("""
            SELECT balance FROM deposit_balance_marks
            WHERE user_id = ? AND chain = ? AND token_symbol = ?
        """, (user_id, chain, token_symbol))
        if row and row['balance'] is not None:
            return Decimal(str(row['balance']))
        row = await self.db.fetch_one("""
            SELECT amount FROM crypto_payments
            WHERE user_id = ? AND chain = ? AND token_symbol = ?
            ORDER BY id DESC LIMIT 1
        """, (user_id, chain, token_symbol))
        if row and row['amount'] is not None:
            try:
                return Decimal(str(row['amount']))
            except (InvalidOperation, ValueError):
                pass
        return Decimal(0)

    async def _write_balance_mark(self, user_id: str, chain: str, token_symbol: str, balance: str):
        await self.db.execute("""
            INSERT INTO deposit_balance_marks (user_id, chain, token_symbol, balance, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, chain, token_symbol)
            DO UPDATE SET balance = excluded.balance, updated_at = excluded.updated_at
        """, (user_id, chain, token_symbol, str(balance)))

    async def _ensure_notifications_table(self):
        """Create `user_notifications` once per monitor instance.

        Called eagerly from `start()` (monitor init), and defensively here
        again from `_notify_deposit_credited` (idempotent, guarded by
        `_notifications_table_ready`) so direct `_process_deposit` callers
        that never invoke `start()` (tests, one-off runs) still work. Either
        way this runs AT MOST ONCE per instance instead of once per deposit.
        """
        if self._notifications_table_ready:
            return
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS user_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                read_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._notifications_table_ready = True

    async def _notify_deposit_credited(self, user_id: str, deposit: Dict, credits: int):
        """Notify the user their deposit was credited (C8).

        Two tiers, both fail-open (money already moved — a notification
        failure must never look like a billing failure):
        1. ALWAYS persist a row to `user_notifications` — durable, pollable
           by any surface/webview later, even if no callback is wired.
        2. If `notify_callback` was injected, best-effort invoke it too.
        """
        message = (
            f"Deposit credited: ${deposit['amount_usd']:.2f} in "
            f"{deposit['token_symbol']} on {deposit['chain']} = {credits} credits"
        )
        try:
            await self._ensure_notifications_table()
            await self.db.execute("""
                INSERT INTO user_notifications (user_id, kind, message)
                VALUES (?, 'deposit_credited', ?)
            """, (user_id, message))
        except Exception as e:
            self.logger.error(f"Failed to persist deposit notification for {user_id}: {type(e).__name__}")

        if self.notify_callback:
            try:
                result = self.notify_callback(user_id, message)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                self.logger.warning(f"notify_callback failed for {user_id} (non-critical): {type(e).__name__}")


# Standalone runner for testing
async def main():
    """Run deposit monitor standalone."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    from core.config import BotConfig
    from core.container import DependencyContainer
    from core.initialization import initialize_core, initialize_auth_services

    # Initialize
    config = BotConfig()
    container = DependencyContainer.get_instance(config)

    await initialize_core(container)
    await initialize_auth_services(container)

    # Get services
    db_manager = container.get_service('database_manager')
    balance_manager = container.get_service('balance_manager')

    if not db_manager or not balance_manager:
        logger.error("Required services not available")
        return

    # Create and start monitor
    monitor = DepositMonitor(db_manager, balance_manager, config)
    await monitor.start()

    try:
        # Run forever
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())
