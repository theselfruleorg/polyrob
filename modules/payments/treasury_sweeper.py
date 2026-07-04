"""Treasury sweeper service for collecting deposits.

Sweeps deposited funds from user addresses to the main treasury
(Gnosis Safe) for secure custody.
"""

import asyncio
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class TreasurySweeper:
    """Sweep deposited funds to treasury address."""

    def __init__(self, db_manager, wallet_generator, config):
        """Initialize treasury sweeper.

        Args:
            db_manager: Database manager instance
            wallet_generator: Wallet generator for signing
            config: Bot configuration
        """
        self.db = db_manager
        self.wallet_gen = wallet_generator
        self.config = config
        self.logger = logging.getLogger('payments.treasury_sweeper')

        # Treasury address (Gnosis Safe)
        self.treasury_address = getattr(config, 'treasury_address', None)

        if not self.treasury_address:
            self.logger.warning("No treasury address configured - sweeping disabled")

        # Sweep interval (seconds)
        self.sweep_interval = getattr(config, 'sweep_interval', 3600)  # 1 hour

        # Minimum balance to trigger sweep (USD)
        self.min_sweep_usd = 50.00

        # Gas price multiplier for priority
        self.gas_multiplier = 1.2

        # Chain configurations
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

        # Token contract addresses (same as deposit monitor)
        self.token_addresses = {
            'ethereum': {
                'USDC': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
                'USDT': '0xdAC17F958D2ee523a2206206994597C13D831ec7'
            },
            'sepolia': {
                'USDC': '0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238',
                'USDT': '0x7169D38820dfd117C3FA1f22a697dBA58d90BA06'
            }
        }

        # Running flag
        self.running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start sweeping deposits."""
        if not self.treasury_address:
            self.logger.error("Cannot start sweeper - no treasury address configured")
            return

        if self.running:
            self.logger.warning("Treasury sweeper already running")
            return

        self.running = True
        self._task = asyncio.create_task(self._sweep_loop())
        self.logger.info(f"💰 Treasury sweeper started (interval: {self.sweep_interval}s)")

    async def stop(self):
        """Stop sweeping deposits."""
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.logger.info("Treasury sweeper stopped")

    async def _sweep_loop(self):
        """Main sweeping loop."""
        while self.running:
            try:
                await self._sweep_all_deposits()
            except Exception as e:
                self.logger.error(f"Error in sweep loop: {e}", exc_info=True)

            # Sleep until next sweep
            await asyncio.sleep(self.sweep_interval)

    async def _sweep_all_deposits(self):
        """Sweep all unswept deposits to treasury."""
        try:
            # Get all deposits that haven't been swept
            deposits = await self.db.fetch_all("""
                SELECT
                    cp.id,
                    cp.user_id,
                    cp.chain,
                    cp.deposit_address,
                    cp.token_symbol,
                    cp.amount,
                    cp.amount_usd,
                    uda.deposit_address as user_address
                FROM crypto_payments cp
                JOIN user_deposit_addresses uda ON cp.user_id = uda.user_id
                WHERE cp.swept_at IS NULL
                  AND cp.status = 'confirmed'
                  AND cp.amount_usd >= ?
                ORDER BY cp.detected_at ASC
                LIMIT 50
            """, (self.min_sweep_usd,))

            if not deposits:
                self.logger.debug("No deposits to sweep")
                return

            self.logger.info(f"Sweeping {len(deposits)} deposits")

            for deposit in deposits:
                try:
                    await self._sweep_deposit(deposit)
                except Exception as e:
                    self.logger.error(
                        f"Error sweeping deposit {deposit['id']}: {e}",
                        exc_info=True
                    )
                    continue

        except Exception as e:
            self.logger.error(f"Error in _sweep_all_deposits: {e}", exc_info=True)

    async def _sweep_deposit(self, deposit: Dict):
        """Sweep a single deposit to treasury.

        Args:
            deposit: Deposit record from database
        """
        try:
            from web3 import Web3

            chain_name = deposit['chain']
            chain_config = self.chains.get(chain_name)

            if not chain_config or not chain_config['rpc_url']:
                self.logger.warning(f"Chain {chain_name} not configured")
                return

            w3 = Web3(Web3.HTTPProvider(chain_config['rpc_url']))
            user_address = deposit['user_address']
            token_symbol = deposit['token_symbol']

            # Get private key for user's deposit address
            private_key = self.wallet_gen.get_private_key_for_user_id(deposit['user_id'])
            account = w3.eth.account.from_key(private_key)

            # Check if ETH or ERC20 token
            if token_symbol == 'ETH':
                tx_hash = await self._sweep_eth(
                    w3, account, user_address, deposit
                )
            else:
                tx_hash = await self._sweep_token(
                    w3, account, user_address, token_symbol, chain_name, deposit
                )

            if tx_hash:
                # Record the sweep
                await self.db.execute("""
                    UPDATE crypto_payments
                    SET swept_at = datetime('now'),
                        sweep_tx_hash = ?
                    WHERE id = ?
                """, (tx_hash, deposit['id']))

                self.logger.info(
                    f"✅ Swept deposit {deposit['id']}: "
                    f"{deposit['amount_usd']:.2f} USD in {token_symbol} on {chain_name} "
                    f"(tx: {tx_hash[:10]}...)"
                )

        except Exception as e:
            self.logger.error(f"Error sweeping deposit {deposit['id']}: {e}", exc_info=True)

    async def _sweep_eth(
        self,
        w3,
        account,
        from_address: str,
        deposit: Dict
    ) -> Optional[str]:
        """Sweep ETH to treasury.

        Args:
            w3: Web3 instance
            account: Account object with private key
            from_address: Source address
            deposit: Deposit record

        Returns:
            Transaction hash if successful
        """
        try:
            # Get balance
            balance = w3.eth.get_balance(from_address)

            if balance == 0:
                self.logger.warning(f"No ETH balance at {from_address}")
                return None

            # Estimate gas
            gas_price = w3.eth.gas_price
            gas_limit = 21000  # Standard ETH transfer

            # Calculate max amount to send (balance minus gas)
            gas_cost = gas_price * gas_limit
            amount_to_send = balance - gas_cost

            if amount_to_send <= 0:
                self.logger.warning(f"Balance too low to cover gas at {from_address}")
                return None

            # Build transaction
            nonce = w3.eth.get_transaction_count(from_address)
            tx = {
                'nonce': nonce,
                'to': self.treasury_address,
                'value': amount_to_send,
                'gas': gas_limit,
                'gasPrice': int(gas_price * self.gas_multiplier),
                'chainId': w3.eth.chain_id
            }

            # Sign and send
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)

            return w3.to_hex(tx_hash)

        except Exception as e:
            self.logger.error(f"Error sweeping ETH: {e}")
            return None

    async def _sweep_token(
        self,
        w3,
        account,
        from_address: str,
        token_symbol: str,
        chain_name: str,
        deposit: Dict
    ) -> Optional[str]:
        """Sweep ERC20 token to treasury.

        Args:
            w3: Web3 instance
            account: Account object with private key
            from_address: Source address
            token_symbol: Token symbol (USDC, USDT)
            chain_name: Chain name
            deposit: Deposit record

        Returns:
            Transaction hash if successful
        """
        try:
            # Get token contract address
            token_address = self.token_addresses.get(chain_name, {}).get(token_symbol)

            if not token_address:
                self.logger.error(f"Token {token_symbol} not configured on {chain_name}")
                return None

            # ERC20 ABI for transfer
            abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function"
                },
                {
                    "constant": False,
                    "inputs": [
                        {"name": "_to", "type": "address"},
                        {"name": "_value", "type": "uint256"}
                    ],
                    "name": "transfer",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function"
                }
            ]

            contract = w3.eth.contract(address=token_address, abi=abi)

            # Get balance
            balance = contract.functions.balanceOf(from_address).call()

            if balance == 0:
                self.logger.warning(f"No {token_symbol} balance at {from_address}")
                return None

            # Build transfer transaction
            nonce = w3.eth.get_transaction_count(from_address)
            gas_price = w3.eth.gas_price

            tx = contract.functions.transfer(
                self.treasury_address,
                balance
            ).build_transaction({
                'from': from_address,
                'nonce': nonce,
                'gas': 100000,  # Estimate for ERC20 transfer
                'gasPrice': int(gas_price * self.gas_multiplier),
                'chainId': w3.eth.chain_id
            })

            # Check ETH balance for gas
            eth_balance = w3.eth.get_balance(from_address)
            gas_cost = tx['gas'] * tx['gasPrice']

            if eth_balance < gas_cost:
                self.logger.warning(
                    f"Insufficient ETH for gas at {from_address} "
                    f"(need {gas_cost / 10**18:.6f} ETH, have {eth_balance / 10**18:.6f} ETH)"
                )
                return None

            # Sign and send
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)

            return w3.to_hex(tx_hash)

        except Exception as e:
            self.logger.error(f"Error sweeping {token_symbol}: {e}")
            return None


# Standalone runner for testing
async def main():
    """Run treasury sweeper standalone."""
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
    wallet_gen = container.get_service('wallet_generator')

    if not db_manager or not wallet_gen:
        logger.error("Required services not available")
        return

    # Create and start sweeper
    sweeper = TreasurySweeper(db_manager, wallet_gen, config)
    await sweeper.start()

    try:
        # Run forever
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await sweeper.stop()


if __name__ == "__main__":
    asyncio.run(main())
