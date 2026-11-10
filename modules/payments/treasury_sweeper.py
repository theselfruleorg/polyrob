"""Treasury sweeper service for collecting deposits.

Sweeps deposited funds from user addresses to the main treasury
(Gnosis Safe) for secure custody.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

from modules.payments.networks import chain_configs, TOKEN_ADDRESSES, ERC20_TRANSFER_ABI

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

        # Chain configurations + token addresses (shared SSOT with DepositMonitor)
        self.chains = chain_configs(config)
        self.token_addresses = TOKEN_ADDRESSES

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
                self.logger.error(f"Error in sweep loop: {type(e).__name__}")
            try:  # 098 MO-01: a sweep in flight for > 1 h goes to the owner once
                from modules.payments.sweep_state import reap_stuck_sweeps
                await reap_stuck_sweeps(self.db)
            except Exception as e:
                self.logger.error(f"Stuck-sweep check failed: {type(e).__name__}")

            # Sleep until next sweep
            await asyncio.sleep(self.sweep_interval)

    async def _sweep_all_deposits(self):
        """Sweep all unswept deposits to treasury."""
        try:
            from modules.payments.sweep_state import ensure_sweep_schema
            await ensure_sweep_schema(self.db)
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
                  AND cp.sweep_state = 'unswept'
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
                        f"Error sweeping deposit {deposit['id']}: {type(e).__name__}"
                    )
                    continue

        except Exception as e:
            self.logger.error(f"Error in _sweep_all_deposits: {type(e).__name__}")

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

            from core.wallet import submission_journal
            if submission_journal.unresolved():
                raise RuntimeError('Unaccounted submission; reconcile before sweeping')

            # A whole-address transfer can cover several credit records. Pin
            # their identities BEFORE broadcast so a later deposit is never
            # booked against a transfer that preceded it.
            rows = await self.db.fetch_all("""
                SELECT id FROM crypto_payments
                WHERE user_id = ? AND chain = ? AND token_symbol = ?
                  AND swept_at IS NULL AND status = 'confirmed'
                ORDER BY id LIMIT 1001
            """, (deposit['user_id'], chain_name, deposit['token_symbol']))
            ids = tuple(int(row['id']) for row in rows)
            if deposit['id'] not in ids:
                return  # Already covered by an earlier sweep in this batch.
            if len(ids) > 1000:
                raise RuntimeError('Too many pending deposits; reconcile before sweeping')
            deposit = dict(deposit, sweep_deposit_ids=ids)
            # 098 MO-01: claim the whole group (unswept -> sweeping) BEFORE any
            # broadcast; a row another sweeper holds means nothing is sent.
            from modules.payments import sweep_state
            started = int(time.time())
            if not await sweep_state.claim_group(self.db, ids, started):
                self.logger.warning(f"Deposit {deposit['id']}: sweep group is held; skipped")
                return
            deposit['_sweep_started_at'] = started
            try:
                await self._sweep_claimed(deposit, chain_name, chain_config, submission_journal)
            except Exception:
                await self._settle_claim(deposit, None)
                raise

        except Exception as e:
            self.logger.error(f"Error sweeping deposit {deposit['id']}: {type(e).__name__}")

    async def _settle_claim(self, deposit: Dict, tx_hash: Optional[str]) -> None:
        """098 MO-01: a claimed group that was not recorded as swept. Nothing
        broadcast -> back to ``unswept``; a reverted receipt -> ``sweep_failed``
        (told once); an unknown broadcast outcome stays ``sweeping`` (the reaper
        tells the owner, nothing is resent)."""
        from modules.payments import sweep_state
        ids, started = deposit['sweep_deposit_ids'], deposit['_sweep_started_at']
        if deposit.get('_sweep_reverted'):
            if await sweep_state.move_group(self.db, ids, 'sweep_failed', ('sweeping',),
                                            started_at=started):
                sweep_state.emit_sweep_failed(deposit, tx_hash or deposit.get('_sweep_tx'))
        elif not deposit.get('_broadcast'):
            await sweep_state.move_group(self.db, ids, 'unswept', ('sweeping',),
                                         started_at=started)
        elif deposit.get('_sweep_tx'):
            # 098 review (O-02): unknown outcome — keep the tx for the owner.
            await sweep_state.note_sweep_tx(self.db, ids, started, deposit['_sweep_tx'])

    async def _sweep_claimed(self, deposit: Dict, chain_name: str, chain_config: Dict,
                             submission_journal) -> None:
        """Broadcast the sweep of a claimed group and record its outcome."""
        from web3 import Web3
        if getattr(self.wallet_gen, 'remote', False):
            # 066 §5.5: the deposit seed lives in polyrob-signer, which
            # sweeps to the destination pinned in signer.toml. 098 MO-01:
            # there is no receipt on this path — swept_unproven, never
            # booked on the deposit mark.
            # ⚠️ The outcome is UNKNOWN from the moment the request may have
            # reached the signer — except for a definite refusal: the signer
            # sends nothing before it refuses (no fee balance, no pinned token,
            # nonce reuse, "not sent"), and an unsent request never arrived.
            # Those release the group (-> unswept, retried next tick); holding
            # them in `sweeping` blocked every later deposit of the
            # user/chain/token for ever (098 review).
            # ⚠️ The signer sweeps the WHOLE latest balance while the monitor
            # credits at the finalized block: funds that arrived after the
            # last credit would leave uncredited. Wait for the credit.
            if not await self._latest_is_credited(deposit, chain_name, chain_config):
                await self._settle_claim(deposit, None)   # nothing sent -> unswept
                return
            deposit['_broadcast'] = True
            try:
                tx_hash = await asyncio.to_thread(
                    self.wallet_gen.sweep, user_id=deposit['user_id'], chain=chain_name,
                    token_symbol=deposit['token_symbol'],
                    deposit_address=deposit['deposit_address'])
            except Exception as exc:
                if _definitely_not_sent(exc):
                    deposit['_broadcast'] = False
                raise
            await self._record_sweep(deposit, tx_hash, chain_name, state='swept_unproven')
            return

        w3 = Web3(Web3.HTTPProvider(
            chain_config['rpc_url'], request_kwargs={'timeout': 15}))
        if await asyncio.to_thread(lambda: w3.eth.chain_id) != chain_config['chain_id']:
            raise ValueError('Treasury RPC chain does not match configured chain')
        user_address = deposit['user_address']
        token_symbol = deposit['token_symbol']

        # Get private key for user's deposit address
        private_key = self.wallet_gen.get_private_key_for_user_id(deposit['user_id'])
        account = w3.eth.account.from_key(private_key)
        if (account.address.lower() != user_address.lower()
                or user_address.lower() != deposit['deposit_address'].lower()):
            raise ValueError('Deposit address does not match derived signing account')

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
            await self._record_sweep(deposit, tx_hash, chain_name)
            submission_journal.mark_booked(tx_hash)
        else:
            await self._settle_claim(deposit, None)

    async def _latest_is_credited(self, deposit: Dict, chain_name: str,
                                  chain_config: Dict) -> bool:
        """True only when the address's LATEST balance is not above the
        deposit monitor's credited mark. An unreadable balance or no mark is
        not proof: the remote sweep waits for the next tick."""
        from decimal import Decimal
        from modules.payments.deposit_monitor import ensure_marks_schema
        token = deposit['token_symbol']
        await ensure_marks_schema(self.db)
        row = await self.db.fetch_one(
            "SELECT balance FROM deposit_balance_marks "
            "WHERE user_id = ? AND chain = ? AND token_symbol = ?",
            (deposit['user_id'], chain_name, token))
        latest = await self._latest_balance(chain_config, chain_name,
                                            deposit['deposit_address'], token)
        if row is None or row['balance'] is None or latest is None:
            self.logger.warning(
                f"Deposit {deposit['id']}: remote sweep waits — the latest balance or "
                f"the credited mark is unknown")
            return False
        if Decimal(str(latest)) > Decimal(str(row['balance'])):
            self.logger.warning(
                f"Deposit {deposit['id']}: remote sweep waits — the address holds "
                f"{token} not yet credited (latest above the credited mark)")
            return False
        return True

    async def _latest_balance(self, chain_config: Dict, chain_name: str,
                              address: str, token_symbol: str):
        """The address balance at ``latest`` in the monitor's mark units (wei
        for ETH, the 6-decimal token amount otherwise), or None (unknown)."""
        from decimal import Decimal
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(
                chain_config['rpc_url'], request_kwargs={'timeout': 15}))
            if await asyncio.to_thread(lambda: w3.eth.chain_id) != chain_config['chain_id']:
                return None
            address = Web3.to_checksum_address(address)
            if token_symbol == 'ETH':
                return int(await asyncio.to_thread(w3.eth.get_balance, address, 'latest'))
            token_address = self.token_addresses.get(chain_name, {}).get(token_symbol)
            if not token_address:
                return None
            contract = w3.eth.contract(address=token_address, abi=ERC20_TRANSFER_ABI)
            raw = await asyncio.to_thread(
                contract.functions.balanceOf(address).call, block_identifier='latest')
            return Decimal(int(raw)) / 10**6
        except Exception as e:
            self.logger.warning(f"latest balance read failed on {chain_name} ({type(e).__name__})")
            return None

    async def _record_sweep(self, deposit: Dict, tx_hash: str, chain_name: str,
                            state: str = 'swept') -> None:
        """Book the exact pre-broadcast deposit group in one atomic UPDATE
        (``sweeping -> swept | swept_unproven``, on the claim this sweep holds)."""
        from modules.payments import sweep_state
        ids = deposit['sweep_deposit_ids']
        n = await sweep_state.move_group(
            self.db, ids, state, ('sweeping',), started_at=deposit['_sweep_started_at'],
            extra_set="swept_at = datetime('now'), sweep_tx_hash = ?",
            extra_params=(tx_hash,))
        if n != len(ids):
            raise RuntimeError('Sweep bookkeeping did not update the complete deposit group')
        if state != 'swept':
            self.logger.warning(f"Swept deposit group {list(ids)} with no receipt "
                                f"(tx {tx_hash[:10]}...): {state}, not booked on the mark")
            return
        await self._book_outflow(deposit, chain_name)

        self.logger.info(
            f"✅ Swept deposit {deposit['id']}: "
            f"{deposit['amount_usd']:.2f} USD in {deposit['token_symbol']} on {chain_name} "
            f"(tx: {tx_hash[:10]}...)"
        )

    async def _book_outflow(self, deposit: Dict, chain_name: str) -> None:
        """098 MO-01: book what the sweep moved, from its receipt, on the
        deposit monitor's balance mark at the sweep's block — so a deposit that
        lands after the sweep and before the next poll is still credited. Only
        the local signer path has the receipt; fail-open (logged)."""
        receipt = deposit.get('_sweep_receipt')
        if not receipt or receipt.get('block') is None:
            return
        from decimal import Decimal
        from modules.payments.deposit_monitor import book_sweep_outflow
        try:
            gas_wei = int(receipt.get('gas_used') or 0) * int(receipt.get('gas_price') or 0)
            user, block, token = deposit['user_id'], int(receipt['block']), deposit['token_symbol']
            if token == 'ETH':
                await book_sweep_outflow(self.db, user, chain_name, 'ETH',
                                         int(deposit.get('_sweep_out_raw') or 0) + gas_wei, block)
            else:
                out = Decimal(int(deposit.get('_sweep_out_raw') or 0)) / 10**6
                await book_sweep_outflow(self.db, user, chain_name, token, out, block)
                if gas_wei:
                    await book_sweep_outflow(self.db, user, chain_name, 'ETH', gas_wei, block)
        except Exception as e:
            self.logger.error("sweep out-flow not booked on the deposit mark (%s)",
                              type(e).__name__)

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
            gas_price = int(w3.eth.gas_price * self.gas_multiplier)
            gas_limit = 21000  # Standard ETH transfer

            # Calculate max amount to send (balance minus gas)
            gas_cost = gas_price * gas_limit
            amount_to_send = balance - gas_cost

            if amount_to_send <= 0:
                self.logger.warning(f"Balance too low to cover gas at {from_address}")
                return None

            # Build transaction
            nonce = w3.eth.get_transaction_count(from_address, 'pending')
            tx = {
                'nonce': nonce,
                'to': self.treasury_address,
                'value': amount_to_send,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'chainId': self.chains[deposit['chain']]['chain_id']
            }

            # Sign and send
            deposit['_sweep_out_raw'] = amount_to_send  # 098 MO-01
            return await asyncio.to_thread(self._submit_sweep, w3, account, tx, deposit)

        except Exception as e:
            self.logger.error(f"Error sweeping ETH: {type(e).__name__}")
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

            contract = w3.eth.contract(address=token_address, abi=ERC20_TRANSFER_ABI)

            # Get balance
            balance = contract.functions.balanceOf(from_address).call()

            if balance == 0:
                self.logger.warning(f"No {token_symbol} balance at {from_address}")
                return None

            # Build transfer transaction
            nonce = w3.eth.get_transaction_count(from_address, 'pending')
            gas_price = w3.eth.gas_price

            tx = contract.functions.transfer(
                self.treasury_address,
                balance
            ).build_transaction({
                'from': from_address,
                'nonce': nonce,
                'gas': 100000,  # Estimate for ERC20 transfer
                'gasPrice': int(gas_price * self.gas_multiplier),
                'chainId': self.chains[chain_name]['chain_id']
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
            deposit['_sweep_out_raw'] = balance  # 098 MO-01
            return await asyncio.to_thread(self._submit_sweep, w3, account, tx, deposit)

        except Exception as e:
            self.logger.error(f"Error sweeping {token_symbol}: {type(e).__name__}")
            return None

    def _submit_sweep(self, w3, account, tx, deposit):
        """Persist the locally derived hash before broadcast; never retry ambiguity."""
        from eth_utils import keccak
        from core.wallet import submission_journal

        expected_chain = self.chains[deposit['chain']]['chain_id']
        if tx['chainId'] != expected_chain or w3.eth.chain_id != expected_chain:
            raise ValueError('Treasury RPC chain does not match configured chain')
        if (account.address.lower() != deposit['user_address'].lower()
                or account.address.lower() != deposit['deposit_address'].lower()):
            raise ValueError('Deposit address does not match derived signing account')
        reference = submission_journal.reserve_signing(
            'treasury:' + deposit['chain'], account.address, tx['nonce'])
        signed = account.sign_transaction(tx)
        raw = getattr(signed, 'raw_transaction', None)
        if raw is None:  # Older eth-account spelling.
            raw = signed.rawTransaction
        tx_hash = '0x' + keccak(bytes(raw)).hex()
        submission_journal.bind_signed_hash(reference, tx_hash)
        deposit['_broadcast'], deposit['_sweep_tx'] = True, tx_hash  # 098 MO-01
        returned = w3.to_hex(w3.eth.send_raw_transaction(raw))
        if returned.lower() != tx_hash:
            raise RuntimeError('Sweep response hash mismatch; reconcile prepared submission')
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60, poll_latency=1)
        if (receipt.get('status') != 1
                or w3.to_hex(receipt.get('transactionHash')).lower() != tx_hash):
            # 098 MO-01: a mined revert of THIS tx is a definite failure.
            deposit['_sweep_reverted'] = (
                receipt.get('status') == 0
                and w3.to_hex(receipt.get('transactionHash')).lower() == tx_hash)
            raise RuntimeError('Sweep not confirmed successful; reconcile prepared submission')
        # 098 MO-01: what the receipt proves left the address, and where.
        deposit['_sweep_receipt'] = {
            'block': receipt.get('blockNumber'), 'gas_used': receipt.get('gasUsed') or 0,
            'gas_price': receipt.get('effectiveGasPrice') or tx.get('gasPrice') or 0}
        # Only the caller may release the interlock, after recording the sweep.
        return tx_hash


def _definitely_not_sent(exc: BaseException) -> bool:
    """A remote-signer sweep error that proves no transaction left: a refusal
    with a definite code, or a request that never reached the signer. An
    ``internal`` refusal or a request that was sent stays unknown."""
    from core.signer import protocol
    from core.signer.client import SignerRefused, SignerUnavailable
    if isinstance(exc, SignerRefused):
        return exc.code != protocol.INTERNAL
    if isinstance(exc, SignerUnavailable):
        return not exc.sent
    return False
