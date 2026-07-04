"""Deposit monitoring service for automatic credit top-ups.

Monitors blockchain for deposits to user addresses and automatically
credits their accounts.
"""

import asyncio
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from decimal import Decimal

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

        # Supported chains and tokens
        self.chains = {
            'ethereum': {
                'rpc_url': getattr(config, 'ethereum_rpc_url', None),
                'chain_id': 1
            },
            'sepolia': {
                'rpc_url': getattr(config, 'sepolia_rpc_url', None),
                'chain_id': 11155111
            }
        }

        # Token contract addresses (stablecoins)
        self.token_addresses = {
            'ethereum': {
                'USDC': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
                'USDT': '0xdAC17F958D2ee523a2206206994597C13D831ec7'
            },
            'sepolia': {
                'USDC': '0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238',  # Sepolia USDC
                'USDT': '0x7169D38820dfd117C3FA1f22a697dBA58d90BA06'   # Sepolia USDT (example)
            }
        }

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
                self.logger.error(f"Error in deposit monitor loop: {e}", exc_info=True)

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
                        if not chain_config['rpc_url']:
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
                    self.logger.error(f"Error checking address {address}: {e}")
                    continue

        except Exception as e:
            self.logger.error(f"Error in _check_all_deposits: {e}", exc_info=True)

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

            # Check if address has received ETH
            eth_balance = w3.eth.get_balance(address)
            if eth_balance > 0:
                try:
                    eth_price = await self._get_eth_price()
                except Exception as e:
                    self.logger.error(
                        f"ETH price oracle unavailable, skipping ETH deposit check "
                        f"for {address} this cycle: {e}"
                    )
                    eth_price = None
                if eth_price is not None:
                    eth_deposit = {
                        'chain': chain_name,
                        'token_symbol': 'ETH',
                        # Price-INDEPENDENT identifier for the dedup guard in
                        # `_process_deposit` (see that method's comment): the
                        # wei balance is stable across ticks for an un-swept
                        # deposit, unlike `amount_usd` which is
                        # `eth_price * balance` and now moves every tick
                        # under the live oracle. Without this, the dedup
                        # SELECT never matches twice in a row and the same
                        # on-chain funds re-credit on every check_interval.
                        'amount': str(eth_balance),
                        'amount_wei': eth_balance,
                        'amount_usd': eth_price * (eth_balance / 10**18)
                    }
                    if eth_deposit['amount_usd'] >= self.min_deposit_usd:
                        deposits.append(eth_deposit)

            # Check each token on this chain
            token_addresses = self.token_addresses.get(chain_name, {})
            for token_symbol, token_address in token_addresses.items():
                balance = await self._get_token_balance(
                    w3,
                    address,
                    token_address
                )

                if balance > 0:
                    # Stablecoins are 1:1 with USD
                    amount_usd = balance

                    if amount_usd >= self.min_deposit_usd:
                        deposits.append({
                            'chain': chain_name,
                            'token_symbol': token_symbol,
                            'amount': str(balance),
                            'amount_usd': amount_usd
                        })

        except Exception as e:
            self.logger.error(f"Error checking {chain_name} for {address}: {e}")

        return deposits

    async def _get_token_balance(self, w3, address: str, token_address: str) -> float:
        """Get ERC20 token balance.

        Args:
            w3: Web3 instance
            address: User address
            token_address: Token contract address

        Returns:
            Token balance as float
        """
        try:
            # ERC20 balanceOf ABI
            abi = [{
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            }]

            contract = w3.eth.contract(address=token_address, abi=abi)
            balance_wei = contract.functions.balanceOf(address).call()

            # USDC/USDT have 6 decimals
            balance = balance_wei / 10**6

            return balance

        except Exception as e:
            self.logger.debug(f"Error getting token balance: {e}")
            return 0.0

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
        try:
            # Check if already processed. Dedup key MUST be price-independent
            # (on-chain amount: wei for ETH, raw token balance for ERC20s) —
            # NEVER amount_usd, which is derived from the live oracle price
            # and changes almost every tick. Keying off amount_usd would
            # never match twice in a row, and the same un-swept balance
            # would re-credit on every check_interval (see C8 review).
            #
            # 'amount' is REQUIRED, never a price-derived fallback (a
            # `deposit.get('amount', str(deposit['amount_usd']))` default
            # here would silently reintroduce the exact CRITICAL dedup bug
            # for any future deposit-dict producer that forgets to set it —
            # let it raise loudly instead).
            deposit_amount = deposit['amount']

            existing = await self.db.fetch_one("""
                SELECT id FROM crypto_payments
                WHERE user_id = ? AND chain = ? AND amount = ? AND token_symbol = ?
            """, (
                user_id,
                deposit['chain'],
                deposit_amount,
                deposit['token_symbol']
            ))

            if existing:
                self.logger.debug(f"Deposit already processed: {existing['id']}")
                return

            # Calculate credits to add
            amount_usd = deposit['amount_usd']
            credits = int(amount_usd / self.credit_rate)

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

                await self.db.connection.commit()
            except Exception:
                await self.db.connection.rollback()
                raise

            self.logger.info(
                f"✅ Processed deposit for {user_id}: "
                f"{amount_usd:.2f} USD ({credits} credits) - "
                f"{deposit['token_symbol']} on {deposit['chain']}"
            )

            await self._notify_deposit_credited(user_id, deposit, credits)

        except Exception as e:
            self.logger.error(f"Error processing deposit for {user_id}: {e}", exc_info=True)

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
            self.logger.error(f"Failed to persist deposit notification for {user_id}: {e}")

        if self.notify_callback:
            try:
                result = self.notify_callback(user_id, message)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                self.logger.warning(f"notify_callback failed for {user_id} (non-critical): {e}")


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
