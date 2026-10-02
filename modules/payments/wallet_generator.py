"""Deposit wallet generator for deterministic user deposit addresses."""

import logging
from eth_account import Account

from core.wallet.derivation import derive_deposit_key

logger = logging.getLogger(__name__)


class DepositWalletGenerator:
    """Generate deterministic deposit addresses for users."""

    def __init__(self, master_seed: str = None):
        """
        Initialize with master seed.

        Args:
            master_seed: Master seed for deterministic key generation.
                        ⚠️ CRITICAL: Must be kept secret and backed up!
                        If lost, cannot sweep funds from deposit addresses.

        Raises:
            ValueError: If master_seed is invalid
        """

        if master_seed and len(master_seed) < 32:
            raise ValueError("Invalid master seed - must be 32+ chars")

        self.master_seed = master_seed
        self.logger = logging.getLogger('payments.wallet_generator')

        if master_seed:
            self.logger.info("Wallet generator initialized with master seed")
        else:
            self.logger.warning("Wallet generator initialized WITHOUT master seed - generation disabled")

    def generate_deposit_address(self, user_id: str) -> str:
        """
        Generate deterministic deposit address for user.

        Same user_id always generates same address.
        This allows us to regenerate private keys for sweeping.

        Args:
            user_id: User ID

        Returns:
            Ethereum address

        Raises:
            ValueError: If master seed not configured
        """

        if not self.master_seed:
            raise ValueError("Master seed not configured - cannot generate deposit addresses")

        # Derive private key from master seed + user_id (the ONE derivation,
        # shared with polyrob-signer — core.wallet.derivation.derive_deposit_key)
        key_material = derive_deposit_key(self.master_seed, user_id)

        # Create Ethereum account
        account = Account.from_key(key_material)

        self.logger.info(f"Generated deposit address for {user_id}: {account.address}")

        return account.address

    def get_account_for_sweep(self, user_id: str) -> Account:
        """
        Regenerate account for sweeping (has private key).

        Args:
            user_id: User ID

        Returns:
            Account object with private key

        Raises:
            ValueError: If master seed not configured
        """

        if not self.master_seed:
            raise ValueError("Master seed not configured - cannot generate accounts")

        key_material = derive_deposit_key(self.master_seed, user_id)

        return Account.from_key(key_material)

    def get_private_key_for_user_id(self, user_id: str) -> bytes:
        """Get private key for a user's deposit address.

        Args:
            user_id: User ID

        Returns:
            Private key bytes

        Raises:
            ValueError: If master seed not configured
        """
        if not self.master_seed:
            raise ValueError("Master seed not configured")

        key_material = derive_deposit_key(self.master_seed, user_id)

        return key_material


class RemoteDepositWalletGenerator:
    """066 §5.5 ``WALLET_SIGNER=remote``: the deposit seed lives in polyrob-signer.

    Same address API as :class:`DepositWalletGenerator`; there is no private key
    here, so the key accessors refuse and the sweeper asks the signer to sweep
    (``deposit.sweep`` — the destination is pinned in ``signer.toml``).
    """

    remote = True

    def __init__(self, client=None):
        if client is None:
            from core.signer.client import SignerClient
            client = SignerClient(timeout=30.0)
        self._client = client
        self.master_seed = None
        self.logger = logging.getLogger('payments.wallet_generator')

    def generate_deposit_address(self, user_id: str) -> str:
        return str(self._client.call("deposit.address", {"user_id": str(user_id)})["address"])

    def sweep(self, *, user_id: str, chain: str, token_symbol: str, deposit_address: str) -> str:
        return str(self._client.call("deposit.sweep", {
            "user_id": str(user_id), "chain": str(chain), "token_symbol": str(token_symbol),
            "deposit_address": str(deposit_address)}, timeout=120.0)["tx_hash"])

    def get_account_for_sweep(self, user_id: str):
        raise ValueError("WALLET_SIGNER=remote: deposit keys live in polyrob-signer")

    def get_private_key_for_user_id(self, user_id: str) -> bytes:
        raise ValueError("WALLET_SIGNER=remote: deposit keys live in polyrob-signer")
